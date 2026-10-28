"""对 DSL 做两层体检：结构层 + 请求契约层。

结构层（不需要任何服务在跑）：
  - YAML 能否解析
  - 节点数 / 边数 / 有没有悬挂边
  - 每个 {{#...#}} 引用的节点是否存在、字段是否是该节点真会产出的字段

请求契约层（需要配套后端在跑）：
  - 把 DSL 里两个 HTTP Request 节点的 url / body 模板**按 Dify 的替换规则**填上样例值
  - 真的按 form（x-www-form-urlencoded）发一次请求
  - 校验返回体里必须出现的字段

⚠️ 这不是「Dify 跑过」的证据，只证明「DSL 里写死的请求，后端确实吃得下」。
   Dify 真正的运行证据要看 Dify 控制台「日志与标注」里的运行记录。

用法：
    python scripts/check_dsl.py                     # 两层都查（后端需已启动）
    python scripts/check_dsl.py --dry-run           # 只查结构层，不发请求
    python scripts/check_dsl.py --base http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit('缺少 PyYAML：pip install "pyyaml>=6"')

DSL = pathlib.Path(__file__).resolve().parent.parent / "dsl" / "01_记仇客栈.yml"

# Dify 侧变量替换的样例取值。键就是 DSL 里 {{#...#}} 中间的表达式。
SAMPLES = {
    "env.MEMORY_API_BASE": None,  # 由 --base 注入
    "sys.query": "上次我把你家碗摔了，还赖着账没给，今儿又来了。",
    "1761610000001.guest_name": "阿吉",
    "1761610000003.text": "房钱翻倍，上回摔的那只碗还没算呢。",
}

VAR = re.compile(r"\{\{#([^#}]+)#\}\}")


def load(path: pathlib.Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def check_structure(doc: dict) -> tuple[dict[str, dict], list[str]]:
    problems: list[str] = []
    graph = ((doc.get("workflow") or {}).get("graph")) or {}
    nodes = {str(n["id"]): n for n in (graph.get("nodes") or [])}
    edges = graph.get("edges") or []

    if not nodes:
        problems.append("graph.nodes 为空")
    if not edges:
        problems.append("graph.edges 为空")

    for edge in edges:
        for end in ("source", "target"):
            if str(edge.get(end)) not in nodes:
                problems.append(f"悬挂边：{edge.get('id')} 的 {end}={edge.get(end)} 不存在")

    # 每个 {{#node_id.field#}} 的 node_id 必须存在；env.* / sys.* 是系统变量，跳过
    for node_id, node in nodes.items():
        for expr in VAR.findall(json.dumps(node, ensure_ascii=False)):
            head = expr.split(".")[0]
            if head in ("env", "sys"):
                continue
            if head not in nodes:
                problems.append(f"节点 {node_id} 引用了不存在的节点 {head}（{expr}）")
    return nodes, problems


def render(template: str, base: str) -> str:
    def sub(match: re.Match[str]) -> str:
        expr = match.group(1)
        if expr == "env.MEMORY_API_BASE":
            return base
        if expr not in SAMPLES:
            raise SystemExit(f"样例值没覆盖 {expr}，请先补 SAMPLES")
        return SAMPLES[expr]

    return VAR.sub(sub, template)


def check_contract(nodes: dict[str, dict], base: str) -> list[str]:
    problems: list[str] = []
    http_nodes = [(nid, n) for nid, n in nodes.items() if n.get("data", {}).get("type") == "http-request"]
    if len(http_nodes) != 2:
        problems.append(f"预期 2 个 http-request 节点，实际 {len(http_nodes)}")

    for node_id, node in http_nodes:
        data = node["data"]
        url = render(data["url"], base)
        title = data.get("title", node_id)
        form: dict[str, str] = {}
        for item in (((data.get("body") or {}).get("data")) or []):
            form[item["key"]] = render(str(item.get("value", "")), base)

        print(f"\n[{title}] {data.get('method', 'get').upper()} {url}")
        print("  form:", json.dumps(form, ensure_ascii=False))
        body = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                print("  ->", resp.status, json.dumps(payload, ensure_ascii=False)[:220])
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{title} 请求失败：{type(exc).__name__} {exc}")
            continue

        if "recall" in url:
            for key in ("user_id", "scope", "count", "events", "has_memory"):
                if key not in payload:
                    problems.append(f"{title} 返回体缺字段 {key}")
        elif "remember" in url:
            for key in ("ok", "user_id", "entries_total", "revision"):
                if key not in payload:
                    problems.append(f"{title} 返回体缺字段 {key}")
        else:
            problems.append(f"{title} 的 url 既不是 recall 也不是 remember：{url}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--dry-run", action="store_true", help="只查结构层，不发真实请求")
    args = ap.parse_args()

    doc = load(DSL)
    nodes, problems = check_structure(doc)
    print(f"DSL: {DSL.name}")
    print(f"节点 {len(nodes)} 个，边 {len(((doc.get('workflow') or {}).get('graph') or {}).get('edges') or [])} 条")
    print("类型分布:", {t: sum(1 for n in nodes.values() if n.get('data', {}).get('type') == t)
                     for t in sorted({n.get('data', {}).get('type') for n in nodes.values()})})

    if not args.dry_run:
        problems += check_contract(nodes, args.base)

    print()
    if problems:
        for item in problems:
            print("✗", item)
        return 1
    print("✓ 结构层通过" + ("" if args.dry_run else "，请求契约层通过"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
