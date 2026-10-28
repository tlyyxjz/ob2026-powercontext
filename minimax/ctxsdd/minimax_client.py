"""MiniMax 接入。

只使用公开 HTTP API（``/text/chatcompletion_v2``），不依赖厂商 SDK。
每次调用都会把「任务、提供的上下文摘要、耗时、token 用量、返回内容」
追加到 ``records/minimax_devlog.md``，作为「MiniMax 实际参与」的证据。

不记录模型的思维链（``reasoning_content``），只记录最终输出。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctxsdd.config import Settings


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class MiniMaxReply:
    content: str
    model: str
    elapsed_s: float
    usage: dict[str, Any]
    raw_id: str


class MiniMaxError(RuntimeError):
    """API 返回了非 0 状态码或不可解析的响应。"""


class MiniMaxClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_key = settings.require_api_key()

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        timeout: float = 180.0,
    ) -> MiniMaxReply:
        payload = {
            "model": self.settings.minimax_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        request = urllib.request.Request(
            f"{self.settings.minimax_base_url}/text/chatcompletion_v2",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - 网络异常路径
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise MiniMaxError(f"HTTP {exc.code}: {detail}") from exc
        elapsed = time.time() - started

        base = body.get("base_resp") or {}
        if base.get("status_code") not in (0, None):
            raise MiniMaxError(
                f"status_code={base.get('status_code')} msg={base.get('status_msg')}"
            )

        choices = body.get("choices") or []
        if not choices:
            raise MiniMaxError(f"响应没有 choices: {json.dumps(body, ensure_ascii=False)[:300]}")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise MiniMaxError("响应 content 为空")

        return MiniMaxReply(
            content=content,
            model=self.settings.minimax_model,
            elapsed_s=round(elapsed, 2),
            usage=body.get("usage") or {},
            raw_id=str(body.get("id", "")),
        )

    # ---------- 记录 ----------

    def log_exchange(
        self,
        *,
        task_id: str,
        purpose: str,
        prompt_summary: dict[str, Any],
        reply: MiniMaxReply,
        output_rel_path: str | None = None,
    ) -> Path:
        """把一次调用追加到开发记录（Markdown，人可直接读）。"""

        settings = self.settings
        settings.records_dir.mkdir(parents=True, exist_ok=True)
        path = settings.records_dir / "minimax_devlog.md"

        usage = reply.usage or {}
        lines = [
            "",
            f"### {_now()} · {purpose}",
            "",
            f"- 任务：`{task_id}`",
            f"- 模型：`{reply.model}`",
            f"- 耗时：{reply.elapsed_s}s",
            f"- token：prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')} total={usage.get('total_tokens')}",
            f"- 请求 ID：`{reply.raw_id}`",
            "- 提供的上下文：",
        ]
        for key, value in prompt_summary.items():
            lines.append(f"  - {key}: {value}")
        if output_rel_path:
            lines.append(f"- 输出已保存：`{output_rel_path}`")
        lines.extend(
            [
                "",
                "<details><summary>模型输出（原文）</summary>",
                "",
                "```",
                reply.content.strip(),
                "```",
                "",
                "</details>",
                "",
            ]
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        return path
