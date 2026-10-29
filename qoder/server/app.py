"""本地后端：标准库 http.server，无 FastAPI/Flask。

所有数据访问只经过 db/store.py，这个文件里不出现一条 SQL。
用法：python server/app.py [--port 8765] [--db-dir PATH]
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(ROOT / "server"))
from store import GOLD_HIT_RATIO, SIMILAR_KEEP_RATIO, SeekdbStore  # noqa: E402
import review  # noqa: E402  点评生成（模型优先，规则兜底）

WEB_DIR = ROOT / "web"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
WRONG_BOOK_MAX = 60

STORE = None
DB_FAULT = None       # {"code": ..., "message": ...}，前端据此出三种错误文案
DB_LOCK = threading.Lock()   # 一个 pymysql 连接被多线程 handler 共用，必须串行

# 三种库侧故障的文案口径（错误态①②）。模型侧的失败态在 review.py 里，不混用。
FAULT_TEXT = {
    "db_not_initialized": "数据目录里没有题库表（错误态①：目录没初始化）",
    "db_locked": "seekdb 实例起不来，库目录不可用或被别的进程占着（错误态②：库文件被占用）",
}


def open_store(db_dir):
    """启动时就把库状态分好类：② 连不上实例，① 连上了但没有表。

    实测：同一个目录被第二个进程 open 不会报错，而是附着到已在运行的实例并复用它的端口，
    所以「被占用」不能靠 seekdb 自己报错，只能看它到底起不起来 / 目录能不能用。
    """
    try:
        store = SeekdbStore(db_dir)
    except Exception as exc:
        return None, {
            "code": "db_locked",
            "message": FAULT_TEXT["db_locked"],
            "detail": f"{type(exc).__name__}: {exc}",
            "hint": f"关掉另一个用这个目录的进程，或换一个目录：python server/app.py --db-dir {db_dir}",
        }
    try:
        store.count("questions")
    except Exception as exc:
        code = "db_not_initialized" if _missing_table(exc) else "db_locked"
        try:
            store.close()
        except Exception:
            pass
        return None, {
            "code": code,
            "message": FAULT_TEXT[code],
            "detail": f"{type(exc).__name__}: {exc}",
            "hint": f"先导题库：python db/init.py --db-dir {db_dir}",
        }
    return store, None


def _missing_table(exc):
    args = getattr(exc, "args", ())
    if args and args[0] == 1146:
        return True
    return "1146" in str(exc) or "doesn't exist" in str(exc)


def next_question(after, today_goal=3):
    """下一题优先「与上一题相似且没练过」，否则退回「当前得分最低」。"""
    if after:
        rows, how = STORE.find_similar(after, limit=8)
        fresh = [r for r in rows if not r["attempt_no"]]
        if fresh:
            return {
                "question": STORE.question(fresh[0]["id"]),
                "reason": f"与题 {after} 同族（{how}，rel={fresh[0]['rel']:.2f}），你还没练过",
                "via": "similar",
                "candidates": [r["id"] for r in fresh],
            }
    plan = STORE.daily_plan(n=today_goal + 3, skip_recent_hours=0)
    for row in plan:
        if not after or row["id"] != after:
            return {
                "question": STORE.question(row["id"]),
                "reason": "没练过" if row["cur_score"] is None else f"当前得分 {row['cur_score']}",
                "via": "plan",
                "candidates": [row["id"]],
            }
    return {"question": None, "reason": "题库已练完", "via": "none", "candidates": []}


def today_progress():
    midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return {"answered_today": STORE.count_answers_since(midnight), "since": midnight}


class Handler(BaseHTTPRequestHandler):
    server_version = "ReviewInterviewer/1.0"

    # ---------- 工具 ----------

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, message, status=400, code=None, extra=None):
        payload = {"ok": False, "error": message}
        if code:
            payload["code"] = code
        if extra:
            payload.update(extra)
        self.send_json(payload, status)

    def db_down(self):
        """库没起来时统一回 503 + 错误码，前端才能出「①没初始化 / ②被占用」两种不同文案。"""
        if DB_FAULT is None:
            return False
        self.fail(DB_FAULT["message"], 503, DB_FAULT["code"],
                  {"hint": DB_FAULT["hint"], "detail": DB_FAULT["detail"]})
        return True

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def param(self, qs, name, cast=int):
        got = qs.get(name)
        if not got:
            return None
        try:
            return cast(got[0])
        except (ValueError, TypeError):
            return None

    def serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        web_root = WEB_DIR.resolve()
        target = (web_root / rel).resolve()
        if not target.is_relative_to(web_root) or not target.is_file():
            self.fail("not found", 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        self.wfile.write(target.read_bytes())

    # ---------- 路由 ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if not route.startswith("/api/"):
            self.serve_static(route)
            return
        qs = parse_qs(parsed.query)
        if self.db_down():
            return
        try:
            with DB_LOCK:
                if route == "/api/plan":
                    n = self.param(qs, "n") or 3
                    prog = today_progress()
                    self.send_json({
                        "ok": True,
                        "plan": STORE.daily_plan(n=n, skip_recent_hours=0),
                        "goal": n,
                        "answered_today": prog["answered_today"],
                        "remaining_today": max(0, n - prog["answered_today"]),
                        "stats": STORE.stats(),
                    })
                elif route == "/api/question":
                    qid = self.param(qs, "id")
                    if qid is None:
                        self.fail("missing id", 400, "missing_id")
                        return
                    q = STORE.question(qid)
                    if q is None:
                        self.fail(f"题 {qid} 不存在", 404, "unknown_question")
                        return
                    self.send_json({"ok": True, "question": q})
                elif route == "/api/similar":
                    qid = self.param(qs, "question_id")
                    if qid is None:
                        self.fail("missing question_id", 400, "missing_question_id")
                        return
                    rows, how = STORE.find_similar(qid, limit=self.param(qs, "limit") or 5)
                    self.send_json({"ok": True, "source": qid, "how": how, "rows": rows})
                elif route == "/api/next":
                    self.send_json({"ok": True, **next_question(self.param(qs, "after"))})
                elif route == "/api/wrongbook":
                    self.send_json({
                        "ok": True,
                        "max_score": WRONG_BOOK_MAX,
                        "rows": STORE.wrong_book(WRONG_BOOK_MAX),
                    })
                elif route == "/api/stats":
                    self.send_json({
                        "ok": True,
                        "connected": True,
                        "db_dir": str(STORE.db_dir),
                        "keep_ratio": SIMILAR_KEEP_RATIO,
                        "gold_ratio": GOLD_HIT_RATIO,
                        "llm": review.public(review.config()),
                        "stats": STORE.stats(),
                    })
                else:
                    self.fail(f"unknown route {route}", 404)
        except Exception as exc:  # 数据库/参数异常统一回结构化错误，前端才能显示对的文案
            self.fail(f"{type(exc).__name__}: {exc}", 500, "server_error")

    def do_POST(self):
        route = urlparse(self.path).path
        try:
            body = self.read_body()
            if route != "/api/submit":
                self.fail(f"unknown route {route}", 404)
                return
            if self.db_down():
                return
            qid, answer = body.get("question_id"), (body.get("answer_text") or "").strip()
            if not isinstance(qid, int) or not answer:
                self.fail("question_id 与 answer_text 都要填", 400, "bad_request")
                return
            # 第一段：只在锁内做读，把模型要看的材料从库里取全（要点来自 ref_point，历史来自 answer_log）
            with DB_LOCK:
                question = STORE.question(qid)
                if question is None:
                    self.fail(f"题 {qid} 不存在", 404, "unknown_question")
                    return
                match_res = STORE.match_ref_points(qid, answer)
                history = STORE.attempts(qid)
            # 第二段：模型调用放在锁外，8 秒超时会把整条数据库通道堵死
            score, rev = review.generate(question, match_res, history, answer)
            # 第三段：先落库、回读确认，前端才许切到下一题
            with DB_LOCK:
                attempt_no = STORE.record_answer(
                    qid, answer, score, json.dumps(rev, ensure_ascii=False, default=str)
                )
                back = STORE.current_score(qid)
                if back["attempt_no"] != attempt_no or back["score"] != score:
                    self.fail("写入后回读不一致", 500, "write_back_mismatch")
                    return
                self.send_json({
                    "ok": True,
                    "persisted": True,
                    "question_id": qid,
                    "attempt_no": attempt_no,
                    "score": score,
                    "answered_at": str(back["answered_at"]),
                    "review": rev,
                })
        except Exception as exc:
            self.fail(f"{type(exc).__name__}: {exc}", 500, "server_error")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main(argv=None):
    global STORE, DB_FAULT
    ap = argparse.ArgumentParser(description="复盘面试官 local server (stdlib http.server)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db-dir", default=str(ROOT / ".data" / "seekdb.db"))
    args = ap.parse_args(argv)

    STORE, DB_FAULT = open_store(args.db_dir)
    if DB_FAULT:
        # 库起不来也要接着监听：静态页要能打开，前端才显示得下错误态①/②的文案
        print(f"库不可用 [{DB_FAULT['code']}] {DB_FAULT['message']}")
        print(f"  detail: {DB_FAULT['detail']}")
        print(f"  hint  : {DB_FAULT['hint']}")
    else:
        print(f"seekdb  : {STORE.db_dir}")
        print(f"stats   : {STORE.stats()}")
    print(f"llm     : {review.public(review.config())}")
    print(f"            （只报模型名与 Key 来源环境变量，Key 本身永不落日志）")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"listening http://{args.host}:{args.port}  (web root: {WEB_DIR})")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if STORE is not None:
            STORE.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
