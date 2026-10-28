"""记仇客栈 · PowerContext 记忆服务。

这是题面 4.4 里说的「配套后端」：把 PowerContext 的 Memory artifact 包成
几个 HTTP 端点，供 Dify 的 HTTP Request 节点调用。它本身不含任何业务
逻辑（谁该说什么话全部由 Dify 的 LLM 节点决定），只负责「记住 / 想起」。

端点
----
POST /recall    读取某个玩家（scope）的历史事件，跨会话有效
POST /remember  追加一条事件
POST /reset     追加一条重置标记，此后 /recall 只返回标记之后的事件
GET  /health    健康检查（同时报告底层库位置与当前条目数）
GET  /history   返回原始条目，供人工核对记忆内容（验收证据用）

所有读写都会追加到 records/memory_api.log，可直接当作「技术使用记录」核对。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from powercontext.builtin.artifacts.memory.models import MemoryEntryInput
from powercontext.builtin.persistence.sqlite.profile import SQLiteConfig
from powercontext.builtin.runtime.composition import open_builtin_contexts
from powercontext.builtin.runtime.config import BuiltinConfig
from powercontext.errors import ArtifactNotFoundError

KIND = "inn-event"
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RECORDS_DIR = ROOT / "records"
DATA_DIR.mkdir(parents=True, exist_ok=True)
RECORDS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("INN_DB_PATH", DATA_DIR / "memory.db")).resolve()
DB_URL = os.environ.get("INN_DB_URL") or f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"
LOG_PATH = RECORDS_DIR / "memory_api.log"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _config() -> BuiltinConfig:
    return BuiltinConfig(database=SQLiteConfig(url=DB_URL))


def _scope(user_id: str) -> str:
    return f"inn/{user_id}"


def _log(action: str, payload: dict[str, Any]) -> None:
    line = json.dumps({"at": _now(), "action": action, **payload}, ensure_ascii=False)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _load(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"event": text}
    return value if isinstance(value, dict) else {"event": text}


async def _read_all(user_id: str) -> list[Any]:
    """按写入顺序返回该 scope 的全部 inn-event 条目（含 reset 标记）。"""

    async with open_builtin_contexts(_config()) as contexts:
        context = await contexts.get(_scope(user_id))
        memory = context.artifacts.memory
        artifact_id = context.artifacts.memory_artifact_id
        try:
            _, entries = await memory.head_entries(artifact_id)
        except ArtifactNotFoundError:
            return []

    rows = [e for e in entries if e.kind == KIND]
    rows.sort(key=lambda e: e.created_in_revision)
    return rows


async def _append(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    async with open_builtin_contexts(_config()) as contexts:
        context = await contexts.get(_scope(user_id))
        memory = context.artifacts.memory
        artifact_id = context.artifacts.memory_artifact_id
        # 第一次写入时 Memory Artifact 还不存在（memory=None 表示新建），
        # 之后必须基于当前 head revision 追加，否则触发版本冲突。
        try:
            head, _ = await memory.head_entries(artifact_id)
        except ArtifactNotFoundError:
            head = None
        await memory.remember(
            memory=head,
            entries=[MemoryEntryInput(kind=KIND, text=text)],
            mode="append",
        )
        _, entries = await memory.head_entries(artifact_id)

    kinds = [e for e in entries if e.kind == KIND]
    kinds.sort(key=lambda e: e.created_in_revision)
    latest = kinds[-1] if kinds else None
    return {
        "scope": _scope(user_id),
        "artifact_id": artifact_id,
        "entries_total": len(kinds),
        "revision": latest.created_in_revision if latest else None,
    }


app = FastAPI(title="Grudge Inn · PowerContext Memory API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RecallIn(BaseModel):
    user_id: str = Field(default="guest-001", description="玩家标识，Dify 里一般传 sys.user_id")
    limit: int = Field(default=20, ge=1, le=200)


class RememberIn(BaseModel):
    user_id: str = "guest-001"
    action: str = Field(default="talk", description="enter/order/smash/brag/help/owe/pay/reset")
    text: str = Field(default="", description="这件事的一句话描述")
    reply: str = Field(default="", description="掌柜当时的回应（便于日后回放）")


class ResetIn(BaseModel):
    user_id: str = "guest-001"


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        entries = await _read_all("__health__")
        ok = True
        err = None
    except Exception as exc:  # noqa: BLE001 - 健康检查要把错误原样报出来
        entries, ok, err = [], False, f"{type(exc).__name__}: {exc}"
    return {
        "status": "ok" if ok else "error",
        "db_url": DB_URL,
        "db_exists": DB_PATH.exists(),
        "probe_entries": len(entries),
        "error": err,
        "at": _now(),
    }


@app.post("/recall")
async def recall(body: RecallIn) -> dict[str, Any]:
    user_id = body.user_id or "guest-001"
    rows = await _read_all(user_id)

    # reset 之后的事件才算「当前这一局」，这样验证样例能一键回到初始状态。
    start = 0
    for index, entry in enumerate(rows):
        if _load(entry.text).get("action") == "reset":
            start = index + 1
    live = rows[start:]

    events = [_load(e.text) for e in live]
    recent = events[-body.limit :]
    _log(
        "recall",
        {
            "user_id": user_id,
            "returned": len(recent),
            "events_in_scope": len(live),
            "revision": live[-1].created_in_revision if live else None,
        },
    )
    return {
        "user_id": user_id,
        "scope": _scope(user_id),
        "count": len(recent),
        "events": recent,
        "has_memory": bool(recent),
    }


@app.post("/remember")
async def remember(body: RememberIn) -> dict[str, Any]:
    user_id = body.user_id or "guest-001"
    payload = {
        "action": body.action,
        "text": body.text,
        "reply": body.reply,
        "at": _now(),
    }
    result = await _append(user_id, payload)
    _log("remember", {"user_id": user_id, **payload, **result})
    # user_id / scope 一律回带：Dify 侧要把它们串到后续节点，
    # 也让调用方能立刻核对「记到哪个名号账上了」。
    return {"ok": True, "user_id": user_id, "scope": _scope(user_id), **result}


@app.post("/reset")
async def reset(body: ResetIn) -> dict[str, Any]:
    user_id = body.user_id or "guest-001"
    result = await _append(
        user_id,
        {"action": "reset", "text": "掌柜把这一页账翻过去了", "reply": "", "at": _now()},
    )
    _log("reset", {"user_id": user_id, **result})
    return {
        "ok": True,
        "user_id": user_id,
        "scope": _scope(user_id),
        "note": "已插入重置标记，之后 /recall 只看新发生的事",
        **result,
    }


# ---------------------------------------------------------------------------
# Dify 用表单编码调用这两个端点。
#
# 为什么不用 JSON：Dify 的 HTTP Request 节点做变量替换时不做 JSON 转义，
# 玩家一句话里带个英文双引号就会把整个 body 破坏掉。x-www-form-urlencoded
# 由 Dify 自己编码，后端交给 FastAPI 解码，绕开这个问题。
# ---------------------------------------------------------------------------


@app.post("/recall_form")
async def recall_form(
    user_id: Annotated[str, Form()] = "",
    limit: Annotated[int, Form()] = 20,
) -> dict[str, Any]:
    return await recall(RecallIn(user_id=user_id, limit=max(1, min(limit, 200))))


@app.post("/remember_form")
async def remember_form(
    user_id: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "talk",
    text: Annotated[str, Form()] = "",
    reply: Annotated[str, Form()] = "",
) -> dict[str, Any]:
    return await remember(
        RememberIn(user_id=user_id, action=action, text=text, reply=reply)
    )


@app.get("/history")
async def history(user_id: str = "guest-001") -> dict[str, Any]:
    rows = await _read_all(user_id)
    return {
        "user_id": user_id,
        "scope": _scope(user_id),
        "total": len(rows),
        "entries": [
            {
                "revision": e.created_in_revision,
                "payload": _load(e.text),
            }
            for e in rows
        ],
    }
