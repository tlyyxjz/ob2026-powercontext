"""P3 自测：subprocess 起后端 -> 等端口就绪 -> 逐个打接口 -> 贴状态码与前 200 字符 -> 杀进程。

服务日志重定向到 .data/selftest-server.log，失败时贴尾部，避免管道写满把服务卡死。
用法：python server/selftest.py [--port 0] [--fresh]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
SELFTEST_DIR = ROOT / ".data" / "selftest.db"
LOG_PATH = ROOT / ".data" / "selftest-server.log"
ANSWER_TEXT = "二级索引叶子只存索引列和主键，要别的列得回聚簇索引查一次，覆盖索引能省掉这次回表"
BAD_ANSWER = "索引就是让查询快一点吧，别的我记不清了"
# 点评相关断言用题 18（读写分离）：它不在题 2 的相似族里，答过它不会把「下一题是相似且没练过」那条搞脏
REVIEW_QID = 18
ANSWER_LIKE = "根因是异步复制有延迟，从库还没重放到最新位点；关键读走主库，或者写后短时间内的读强制走主库"
# 点评固定三段前缀，口径与 server/review.py 的 MARKERS 一致
REVIEW_MARKERS = ("你答到的点：", "你漏掉的点：", "一句话建议：")
MAX_REVIEW_CHARS = 120   # review.fit_length 硬卡：按 render() 后的整段文本计（含前缀与换行）
BASE = ""                # 由 main() 按实际端口赋值，供 engine_visible() 使用


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(host, port, deadline, log):
    """端口通了不代表库起来了，所以以 /api/stats 真正返回为准。"""
    start = time.time()
    last = ""
    while time.time() - start < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/api/stats", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    print(f"   等待超时，最后一次错误：{last}")
    print("   ---- 服务日志尾部 ----")
    print(tail(log))
    return False


def tail(path, lines=25):
    if not pathlib.Path(path).exists():
        return "(无日志)"
    data = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])


def call(method, url, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers.get("Content-Type", "")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}", ""


def review_shape(rev):
    """点评必须是三段、每段带固定前缀，且整段不超过 120 字（含前缀）。"""
    if not isinstance(rev, dict):
        return False, f"review 不是 dict，而是 {type(rev).__name__}"
    text = rev.get("text") or ""
    if not text:
        return False, "review.text 为空"
    missing = [m for m in REVIEW_MARKERS if m not in text]
    if missing:
        return False, f"缺段 {missing}；实际 text={text[:100]!r}"
    if len(text) > MAX_REVIEW_CHARS:
        return False, f"超长 {len(text)} > {MAX_REVIEW_CHARS}"
    if not rev.get("engine"):
        return False, "review 没有 engine 字段，无法证明走了 seekdb 规则或模型"
    return True, ""


def no_secret(text):
    """响应里不能出现密钥：不是只查字面 'sk-'，还要拿本机真实 Key 做子串比对。"""
    raw = text or ""
    if "sk-" in raw:
        return False, "响应里出现 'sk-' 字样"
    for name in ("LLM_API_KEY", "DEEPSEEK_API_KEY"):
        val = os.environ.get(name) or ""
        if len(val) >= 8 and val in raw:
            return False, f"响应里出现 {name} 的真实值"
    return True, ""


def engine_visible():
    """stats 要能报出点评引擎（只有模型名和凭据来源名，不含 Key）。"""
    status, text, _ = call("GET", BASE + "/api/stats")
    if status != 200:
        return False, f"/api/stats 状态 {status}"
    try:
        doc = json.loads(text)
    except Exception as exc:
        return False, f"stats 不是 JSON：{exc}"
    llm = doc.get("llm") or {}
    if not llm.get("model"):
        return False, f"stats.llm 里没有 model：{llm}"
    leaked, why = no_secret(text)
    if not leaked:
        return False, why
    return True, ""


def show(label, status, text, ctype="", limit=200, expect=200):
    flag = "OK " if status == expect else "FAIL"
    want = "" if status == expect else f"  (期望 {expect})"
    print(f"[{flag}] {status!s:<4} {label}{want}")
    if ctype:
        print(f"        content-type: {ctype}")
    preview = " ".join(text.split())
    if len(preview) > limit:
        preview = preview[:limit] + f"...（共 {len(text)} 字符）"
    print("        " + preview)
    return status == expect


def main(argv=None):
    ap = argparse.ArgumentParser(description="spin up the server and hit every endpoint")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--db-dir", default=str(SELFTEST_DIR))
    ap.add_argument("--fresh", action="store_true", help="先删掉自检库目录")
    ap.add_argument("--deadline", type=float, default=180.0)
    args = ap.parse_args(argv)

    db_dir = pathlib.Path(args.db_dir)
    if args.fresh and db_dir.exists():
        import shutil

        shutil.rmtree(db_dir)
        print(f"[准备] 已清空 {db_dir}")
    port = args.port or free_port()

    print(f"[1/4] 初始化自检库（python db/init.py --db-dir {db_dir.name}）")
    init = subprocess.run(
        [sys.executable, str(ROOT / "db" / "init.py"), "--db-dir", str(db_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print((init.stdout or "").strip() or "(init 无输出)")
    if init.returncode != 0:
        print("init 失败，服务日志如下：\n" + (init.stderr or ""))
        return 1

    print(f"\n[2/4] 起服务 python server/app.py --port {port}（首次建库要拷 seekdb.exe，可能等十几秒）")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logf = open(LOG_PATH, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "app.py"), "--host", args.host,
         "--port", str(port), "--db-dir", str(db_dir)],
        stdout=logf, stderr=subprocess.STDOUT,
    )
    base = f"http://{args.host}:{port}"
    global BASE
    BASE = base
    failed = 0
    try:
        if not wait_port(args.host, port, args.deadline, LOG_PATH):
            return 1
        print(f"        就绪：{base}\n")

        print("[3/4] 逐个打接口")
        checks = [
            ("GET  /api/plan?n=3", "GET", "/api/plan?n=3", None, 200),
            ("GET  /api/question?id=2", "GET", "/api/question?id=2", None, 200),
            ("POST /api/submit 题2（答得全）", "POST", "/api/submit",
             {"question_id": 2, "answer_text": ANSWER_TEXT}, 200),
            ("POST /api/submit 题2（故意答差，要进错题本）", "POST", "/api/submit",
             {"question_id": 2, "answer_text": BAD_ANSWER}, 200),
            ("POST /api/submit 空回答（必须被挡）", "POST", "/api/submit",
             {"question_id": 2, "answer_text": "   "}, 400),
            ("POST /api/submit 不存在的题（必须被挡）", "POST", "/api/submit",
             {"question_id": 9999, "answer_text": "随便写点"}, 404),
            ("GET  /api/similar?question_id=2", "GET", "/api/similar?question_id=2", None, 200),
            ("GET  /api/next?after=2", "GET", "/api/next?after=2", None, 200),
            ("GET  /api/wrongbook", "GET", "/api/wrongbook", None, 200),
            ("GET  /api/stats", "GET", "/api/stats", None, 200),
            ("GET  /  (静态 index.html)", "GET", "/", None, 200),
            ("GET  /app.js (静态资源)", "GET", "/app.js", None, 200),
            ("GET  /api/nope（未知路由）", "GET", "/api/nope", None, 404),
            ("GET  /../../../windows/win.ini（路径穿越）", "GET", "/../../../windows/win.ini", None, 404),
        ]
        for label, method, path, body, expect in checks:
            status, text, ctype = call(method, base + path, body)
            if not show(label, status, text, ctype, expect=expect):
                failed += 1

        print("\n[3.5] 产品口径断言（不是只看 200）")
        # 点评相关的断言要一份真实 submit 响应，用题 18（跟题 2 的相似族无关）
        status, sub_text, _ = call("POST", base + "/api/submit",
                                   {"question_id": REVIEW_QID, "answer_text": ANSWER_LIKE})
        sub = json.loads(sub_text) if status == 200 else {}
        if status != 200:
            print(f"[FAIL] 预取 submit 响应（题{REVIEW_QID}）状态 {status}")
            print("       " + " ".join(sub_text.split())[:200])
            failed += 1
        else:
            rev = sub.get("review") or {}
            print(f"       题{REVIEW_QID} 真实点评：engine={rev.get('engine')} "
                  f"model={rev.get('model')} 得分={sub.get('score')} 耗时={rev.get('elapsed_ms')}ms")
            for line in (rev.get("text") or "").split("\n"):
                print("       | " + line)
        for name, ok in [
            ("点评输出严格三段且不超过 120 字", lambda: review_shape(sub.get("review"))),
            ("响应与入库点评里没有任何密钥", lambda: no_secret(sub_text)),
            ("stats 报出点评引擎（只有模型名，没有 Key）", lambda: engine_visible()),
        ]:
            try:
                passed, why = ok()
            except Exception as exc:
                passed, why = False, f"{type(exc).__name__}: {exc}"
            print(f"[{'OK ' if passed else 'FAIL'}] {name}" + ("" if passed else f" -> {why}"))
            if not passed:
                failed += 1
        for name, path, ok in [
            ("相似题走全文而不是兜底", "/api/similar?question_id=2",
             lambda d: d["how"] == "fulltext" and d["rows"] and all(r["rel"] > 0 for r in d["rows"])),
            ("下一题是相似且没练过的题", "/api/next?after=2",
             lambda d: d["via"] == "similar" and d["question"]["attempt_no"] == 0),
            ("答差的那道题进了错题本", "/api/wrongbook",
             lambda d: [r["id"] for r in d["rows"]] and 2 in [r["id"] for r in d["rows"]]),
            ("今日进度随作答变化", "/api/plan?n=3",
             lambda d: d["answered_today"] >= 2 and d["remaining_today"] <= 1),
        ]:
            status, text, _ = call("GET", base + path)
            try:
                passed = status == 200 and ok(json.loads(text))
                if not passed:
                    why = " ".join(text.split())[:200]
            except Exception as exc:
                passed, why = False, f"{type(exc).__name__}: {exc}"
            print(f"[{'OK ' if passed else 'FAIL'}] {name}")
            if not passed:
                print("        " + why)
                failed += 1

        print(f"\n结果：{len(checks)} 个接口 + 7 条产品断言，失败 {failed} 项")
        if failed:
            print("\n---- 服务日志尾部 ----")
            print(tail(LOG_PATH))
        return 1 if failed else 0
    finally:
        print("\n[4/4] 杀进程")
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        logf.close()
        print(f"        服务已停止（terminate 后 returncode={proc.returncode}），完整日志：{LOG_PATH}")


if __name__ == "__main__":
    raise SystemExit(main())
