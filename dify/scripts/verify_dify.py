"""在本地 Dify 上把这条 DSL 跑一遍，并逐节点打印状态与输出。

它做四件事：
  1. 登录（1.18 的 password 字段要 base64）
  2. 导入 DSL（/console/api/apps/imports，要 CSRF 头）
  3. 发布（不发布的话 /v1 接口会报 Workflow not published）
  4. 跑一次并消费 SSE，打印每个节点 status 与 outputs

前提：
  - Dify 已起来（默认 http://127.0.0.1:8080）
  - docker/.env 里 SSRF_PROXY_ALLOW_PRIVATE_IPS 已包含宿主机网段，且 ssrf_proxy 已重建
  - 「掌柜的嘴」节点选好了模型（本机验证时用的是本地假模型，见 mock_llm.py）

用法：
    python scripts/verify_dify.py
    python scripts/verify_dify.py --base http://127.0.0.1:8080 --guest 阿七
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys

import requests

DSL = pathlib.Path(__file__).resolve().parent.parent / "dsl" / "01_记仇客栈.yml"


def login(base: str, email: str, password: str) -> requests.Session:
    S = requests.Session()
    S.trust_env = False
    S.proxies = {}
    r = S.post(
        f"{base}/console/api/login",
        json={"email": email,
              "password": base64.b64encode(password.encode()).decode(),
              "language": "zh-Hans", "remember_me": True},
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"登录失败 {r.status_code}: {r.text[:200]}")
    return S


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--email", default="verify@local.test")
    ap.add_argument("--password", default="VerifyPass123!")
    ap.add_argument("--guest", default="阿七")
    ap.add_argument("--query", default="我又来了，还有面吗？")
    args = ap.parse_args()

    S = login(args.base, args.email, args.password)
    H = {"X-CSRF-Token": S.cookies.get("csrf_token") or "", "Content-Type": "application/json"}

    r = S.post(f"{args.base}/console/api/apps/imports", headers=H,
               json={"mode": "yaml-content", "yaml_content": DSL.read_text(encoding="utf-8")},
               timeout=180)
    print("import:", r.status_code, r.text[:200])
    body = r.json()
    app_id = body.get("app_id")
    if not app_id:
        sys.exit("导入没拿到 app_id")

    publish = S.post(f"{args.base}/console/api/apps/{app_id}/workflows/publish", headers=H, json={}, timeout=120)
    print("publish:", publish.status_code)

    r = S.post(f"{args.base}/console/api/apps/{app_id}/advanced-chat/workflows/draft/run", headers=H,
               json={"inputs": {"guest_name": args.guest}, "query": args.query, "files": []},
               stream=True, timeout=300)
    print("run:", r.status_code)
    failed = 0
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        kind, data = ev.get("event"), ev.get("data", {})
        if kind == "node_finished":
            title = str(data.get("title") or data.get("node_type"))
            status = data.get("status")
            print(f"  {title[:26]:28s} {status:10s} {json.dumps(data.get('outputs'), ensure_ascii=False)[:140]}")
            if status != "succeeded":
                failed += 1
                print("     error:", str(data.get("error"))[:200])
        elif kind == "workflow_finished":
            print("workflow:", data.get("status"),
                  "| answer:", json.dumps((data.get("outputs") or {}).get("answer"), ensure_ascii=False)[:200])

    print("\n结论:", "全部节点通过" if failed == 0 else f"{failed} 个节点失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
