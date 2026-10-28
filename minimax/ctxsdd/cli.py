"""ctxsdd 命令行入口。

六个子命令覆盖完整的 SDD 闭环：

    init      初始化 seekdb 资料库
    import    导入规格 / 代码符号 / 测试用例
    context   按任务输出上下文（并合并 PowerContext 中的任务状态）
    state     保存或读取任务状态
    propose   调用 MiniMax 依据上下文生成代码修改
    verify    运行测试并把验证结果写入任务状态
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ctxsdd import agent
from ctxsdd.config import ROOT, Settings
from ctxsdd.importer import build_rows
from ctxsdd.state import TaskStateStore, append_state_log
from ctxsdd.store import Store


def _settings(args: argparse.Namespace) -> Settings:
    return Settings(
        project_dir=Path(args.project).resolve() if getattr(args, "project", None) else Settings().project_dir,
        specs_dir=Path(args.specs).resolve() if getattr(args, "specs", None) else Settings().specs_dir,
    )


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# --------------------------- init / import ---------------------------


def cmd_init(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = Store(settings)
    info = store.init_schema()
    _print_json({"ok": True, **info, "tables": store.counts()})
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = Store(settings)

    spec_rows, symbol_rows, case_rows, versions = build_rows(
        specs_dir=settings.specs_dir, project_dir=settings.project_dir
    )
    if not spec_rows:
        print(f"未在 {settings.specs_dir} 找到可解析的规格条目", file=sys.stderr)
        return 2

    run_id = args.run_id or f"import-{len(versions)}v"
    counts = store.replace_corpus(
        run_id=run_id,
        spec_rows=spec_rows,
        symbol_rows=symbol_rows,
        case_rows=case_rows,
        spec_versions=versions,
    )
    _print_json(
        {
            "ok": True,
            "run_id": run_id,
            "spec_versions": sorted(set(versions)),
            "imported": counts,
            "spec_item_ids": [row["id"] for row in spec_rows],
        }
    )
    return 0


# --------------------------- context ---------------------------


def _verify_sources(payload: dict[str, Any], settings: Settings) -> list[str]:
    """校验上下文中引用的来源文件真实存在，返回缺失列表。

    解析基准：
    - 规格路径相对本模块根（``minimax/``）；
    - 代码与测试路径相对业务工程根（``demo_project/``）。
    """

    missing: list[str] = []

    for row in payload["specs"]:
        raw = row["source_path"]
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (ROOT / raw).resolve()
        if not candidate.exists():
            missing.append(raw)

    for group in ("symbols", "tests"):
        for row in payload[group]:
            raw = row["path"]
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = (settings.project_dir / raw).resolve()
            if not candidate.exists():
                missing.append(raw)

    return sorted(set(missing))


def build_context(settings: Settings, *, task: str, task_id: str | None) -> dict[str, Any]:
    store = Store(settings)
    selection = store.select(task_text=task)

    payload: dict[str, Any] = {
        "task": task,
        "keywords": selection["keywords"],
        "specs": [
            {
                "id": row["id"],
                "title": row["title"],
                "source_path": row["source_path"],
                "body": row["body"],
            }
            for row in selection["specs"]
        ],
        "symbols": [
            {
                "id": row["id"],
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "signature": row["signature"],
            }
            for row in selection["symbols"]
        ],
        "tests": [
            {
                "id": row["id"],
                "path": row["path"],
                "name": row["name"],
                "covers": row["covers"],
            }
            for row in selection["tests"]
        ],
    }

    state = None
    if task_id:
        state = asyncio.run(TaskStateStore(settings).load(task_id))
    payload["state"] = state
    payload["sources"] = {
        "specs": sorted({row["source_path"] for row in payload["specs"]}),
        "code": sorted({row["path"] for row in payload["symbols"]}),
        "tests": sorted({row["path"] for row in payload["tests"]}),
    }
    payload["missing_sources"] = _verify_sources(payload, settings)
    return payload


def cmd_context(args: argparse.Namespace) -> int:
    settings = _settings(args)
    task = args.task or (
        Path(args.task_file).read_text(encoding="utf-8") if args.task_file else ""
    )
    if not task.strip():
        print("需要 --task 或 --task-file", file=sys.stderr)
        return 2

    payload = build_context(settings, task=task, task_id=args.task_id)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"上下文已写入 {out_path}")

    print(f"\n任务：{payload['task'].strip().splitlines()[0]}")
    print(f"关键词：{', '.join(payload['keywords'][:12])}")
    print(f"\n适用规格（{len(payload['specs'])} 条）：")
    for row in payload["specs"]:
        print(f"  - [{row['id']}] {row['title']}  ← {row['source_path']}")
    print(f"\n相关代码（{len(payload['symbols'])} 个符号）：")
    for row in payload["symbols"]:
        print(f"  - {row['symbol']}  {row['path']}:{row['start_line']}-{row['end_line']}")
    print(f"\n验证测试（{len(payload['tests'])} 个）：")
    for row in payload["tests"]:
        covers = f"  覆盖 {row['covers']}" if row["covers"] else ""
        print(f"  - {row['name']}  ({row['path']}){covers}")
    if payload["state"]:
        print("\n任务状态（来自 PowerContext）：")
        print(f"  - revision {payload['state'].get('_revision')}，更新于 {payload['state'].get('updated_at')}")
        for item in payload["state"].get("done", []):
            print(f"  - 已完成：{item}")
        for item in payload["state"].get("todo", []):
            print(f"  - 待办：{item}")
    if payload["missing_sources"]:
        print(f"\n⚠️ 以下来源不存在：{payload['missing_sources']}", file=sys.stderr)
    return 0 if not payload["missing_sources"] else 3


# --------------------------- state ---------------------------


def cmd_state(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = TaskStateStore(settings)

    if args.action == "show":
        state = asyncio.run(store.load(args.task_id))
        if state is None:
            print(f"任务 {args.task_id} 尚无保存的状态", file=sys.stderr)
            return 4
        _print_json(state)
        append_state_log(settings, args.task_id, "load", {"revision": state.get("_revision")})
        return 0

    # save
    state: dict[str, Any] = {
        "spec_version": args.spec_version,
        "done": list(args.done or []),
        "todo": list(args.todo or []),
        "verifications": [],
    }
    if args.merge_from:
        previous = asyncio.run(store.load(args.task_id)) or {}
        state["done"] = sorted(set(previous.get("done", [])) | set(state["done"]))
        state["todo"] = sorted(set(previous.get("todo", [])) | set(state["todo"]))
        state["verifications"] = previous.get("verifications", [])
    if args.verification:
        state["verifications"].append(args.verification)

    receipt = asyncio.run(store.save(args.task_id, state))
    append_state_log(settings, args.task_id, "save", {**receipt, "state": state})
    _print_json({"ok": True, "saved": state, **receipt})
    return 0


# --------------------------- propose / apply ---------------------------


def cmd_propose(args: argparse.Namespace) -> int:
    settings = _settings(args)
    task_text = Path(args.task_file).read_text(encoding="utf-8") if args.task_file else (args.task or "")
    if not task_text.strip():
        print("需要 --task 或 --task-file", file=sys.stderr)
        return 2

    payload = build_context(settings, task=task_text, task_id=args.task_id)
    spec_items = payload["specs"]
    if not spec_items:
        print("上下文里没有规格条目，无法生成", file=sys.stderr)
        return 3

    source_path = (settings.project_dir / args.source).resolve()
    if not source_path.exists():
        print(f"源码不存在：{source_path}", file=sys.stderr)
        return 3

    test_names = [row["name"] for row in payload["tests"]] or [args.function]

    result = agent.propose(
        settings,
        task_id=args.task_id,
        task=task_text,
        spec_items=spec_items,
        source_path=source_path,
        function_name=args.function,
        test_names=test_names,
    )

    print(f"MiniMax 返回 {len(result.code)} 字符，耗时 {result.reply.elapsed_s}s")
    print(f"原始输出：records/{args.task_id}.minimax_raw.txt")
    print(f"补丁代码：records/{args.task_id}.minimax_patch.py")
    print(f"差异文件：records/{args.task_id}.minimax.diff")
    if args.diff:
        print("\n" + result.diff)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    settings = _settings(args)
    patch_path = settings.records_dir / f"{args.task_id}.minimax_patch.py"
    if not patch_path.exists():
        print(f"没有找到补丁：{patch_path}", file=sys.stderr)
        return 3
    source_path = (settings.project_dir / args.source).resolve()
    diff = agent.apply_patch(
        source_path, function_name=args.function, new_code=patch_path.read_text(encoding="utf-8")
    )
    applied_path = settings.records_dir / f"{args.task_id}.applied.diff"
    applied_path.write_text(diff, encoding="utf-8")
    print(f"已应用到 {source_path}")
    print(f"差异已保存 {applied_path}")
    print("\n" + diff)
    return 0


# --------------------------- verify ---------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    settings = _settings(args)
    project = settings.project_dir
    cmd = [sys.executable, "-m", "pytest", "-q", args.path or "tests"]
    proc = subprocess.run(cmd, cwd=project, capture_output=True, text=True)
    tail = "\n".join((proc.stdout or proc.stderr).strip().splitlines()[-6:])

    summary = {
        "cmd": " ".join(cmd),
        "cwd": str(project),
        "exit_code": proc.returncode,
        "tail": tail,
    }

    log_path = settings.records_dir / f"{args.task_id}.verify.log"
    settings.records_dir.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"$ {' '.join(cmd)}\n# cwd: {project}\n# exit_code: {proc.returncode}\n\n"
        f"{proc.stdout}\n{proc.stderr}",
        encoding="utf-8",
    )

    if args.task_id and args.write_state:
        store = TaskStateStore(settings)
        previous = asyncio.run(store.load(args.task_id)) or {}
        state = {
            "spec_version": args.spec_version or previous.get("spec_version"),
            "done": previous.get("done", []),
            "todo": previous.get("todo", []),
            "verifications": previous.get("verifications", [])
            + [
                {
                    "cmd": summary["cmd"],
                    "exit_code": proc.returncode,
                    "summary": tail.replace("\n", " | ")[-300:],
                    "at": summary.get("cwd"),
                }
            ],
        }
        receipt = asyncio.run(store.save(args.task_id, state))
        append_state_log(settings, args.task_id, "verify", {**receipt, "exit_code": proc.returncode})

    _print_json({"ok": proc.returncode == 0, **summary})
    return proc.returncode


# --------------------------- main ---------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctxsdd",
        description="面向 SDD 的工程上下文模块：规格 / 代码 / 测试检索 + 任务状态复用 + MiniMax 改码",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = dict()
    p_init = sub.add_parser("init", help="初始化 seekdb（建库建表）")
    p_init.add_argument("--project", **common)
    p_init.set_defaults(func=cmd_init)

    p_import = sub.add_parser("import", help="导入规格、代码符号与测试用例")
    p_import.add_argument("--project")
    p_import.add_argument("--specs")
    p_import.add_argument("--run-id")
    p_import.set_defaults(func=cmd_import)

    p_ctx = sub.add_parser("context", help="按任务输出上下文")
    p_ctx.add_argument("--task")
    p_ctx.add_argument("--task-file")
    p_ctx.add_argument("--task-id")
    p_ctx.add_argument("--project")
    p_ctx.add_argument("--specs")
    p_ctx.add_argument("--out")
    p_ctx.set_defaults(func=cmd_context)

    p_state = sub.add_parser("state", help="保存或读取任务状态（PowerContext）")
    p_state.add_argument("action", choices=["save", "show"])
    p_state.add_argument("--task-id", required=True)
    p_state.add_argument("--spec-version")
    p_state.add_argument("--done", action="append")
    p_state.add_argument("--todo", action="append")
    p_state.add_argument("--verification")
    p_state.add_argument("--merge-from", action="store_true")
    p_state.add_argument("--project")
    p_state.set_defaults(func=cmd_state)

    p_prop = sub.add_parser("propose", help="让 MiniMax 依据上下文生成修改")
    p_prop.add_argument("--task-id", required=True)
    p_prop.add_argument("--task")
    p_prop.add_argument("--task-file")
    p_prop.add_argument("--source", required=True, help="相对 demo_project 的源码路径")
    p_prop.add_argument("--function", required=True)
    p_prop.add_argument("--project")
    p_prop.add_argument("--specs")
    p_prop.add_argument("--diff", action="store_true")
    p_prop.set_defaults(func=cmd_propose)

    p_apply = sub.add_parser("apply", help="应用 MiniMax 生成的补丁")
    p_apply.add_argument("--task-id", required=True)
    p_apply.add_argument("--source", required=True)
    p_apply.add_argument("--function", required=True)
    p_apply.add_argument("--project")
    p_apply.set_defaults(func=cmd_apply)

    p_verify = sub.add_parser("verify", help="运行测试并记录结果")
    p_verify.add_argument("--task-id")
    p_verify.add_argument("--path")
    p_verify.add_argument("--spec-version")
    p_verify.add_argument("--write-state", action="store_true")
    p_verify.add_argument("--project")
    p_verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
