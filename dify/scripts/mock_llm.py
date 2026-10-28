"""本地假模型服务：只为验证 Dify 的 LLM 节点能连上、能拿到回复。

它说的是「掌柜的口吻」，但内容是按账本规则拼的固定话术——**不是真模型**。
本机没有可用的大模型 Key，这一步只为证明节点链路通，不冒充模型效果。

启动：
    python mock_llm.py            # 监听 8766
Dify 里配置：base_url = http://host.docker.internal:8766/v1，api_key 随便填
"""

from __future__ import annotations

import json
import pathlib
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = pathlib.Path(__file__).resolve().parent / "mock_llm.log"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/v1/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "MiniMax-M3.1-Flash", "object": "model", "owned_by": "mock"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            payload = {}
        messages = payload.get("messages") or []
        system = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        user = "".join(m.get("content", "") for m in messages if m.get("role") == "user")
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "model": payload.get("model"), "user": user[:120],
                                 "prompt_has_ledger": "账本" in system or "events" in system},
                                ensure_ascii=False) + "\n")

        if "砸" in system or "掀" in system or "赖账" in system:
            reply = "又是你。上回掀桌子那笔账我可记着，房钱翻倍，先赔碗再谈住不住。"
        elif "帮忙" in system:
            reply = "哟，是您呐。上回多亏您挑水，账上记着呢，今儿给您留了靠窗的位子。"
        else:
            reply = "面生得很。打哪儿来的？先报个名号，我这店不招呼生人。"

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for piece in [reply[i:i + 6] for i in range(0, len(reply), 6)]:
                chunk = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                         "created": int(time.time()), "model": payload.get("model", "mock"),
                         "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            done = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": payload.get("model", "mock"),
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            self.wfile.write(b"data: " + json.dumps(done, ensure_ascii=False).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return

        self._json(200, {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", "mock"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8766), Handler).serve_forever()
