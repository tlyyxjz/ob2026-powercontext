"""一键验收：从零开始跑完 SDD 闭环，并逐项自检。

用法（在 ``minimax/`` 目录下）：

    python scripts/run_acceptance.py

前置：
- seekdb 已在 127.0.0.1:2881 运行；
- 已 ``pip install -e ".[builtin]"`` 安装 PowerContext（在仓库根执行）；
- 已设置 ``MINIMAX_API_KEY``。

脚本会重建资料库与状态库，因此每次执行结果一致（唯一变量是模型返回的代码内容）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE_DB = ROOT / "var" / "task_state.db"

TASK_ID = "T-001"

# 让子进程既能导入 ctxsdd，也能导入 PowerContext：
#   - vendor/：随本目录交付的 PowerContext 源码（见 vendor/NOTICE.md），排在最前，
#     这样评审直接跑本脚本时不必再手工设置 PYTHONPATH；
#   - ../src 与 POWERCONTEXT_SRC：兼容「用 pip install -e 的方式装了源码仓库」的场景。
_ENV = os.environ.copy()
_VENDOR = str(ROOT / "vendor")
_SRC = os.environ.get("POWERCONTEXT_SRC") or str(ROOT.parent / "src")
_PATH_ENTRIES = [str(ROOT), _VENDOR, _SRC, _ENV.get("PYTHONPATH", "")]
_ENV["PYTHONPATH"] = os.pathsep.join(entry for entry in _PATH_ENTRIES if entry)
_ENV.setdefault("PYTHONIOENCODING", "utf-8")


def run(cmd: list[str], *, expect: int | None = 0, label: str) -> subprocess.CompletedProcess:
    print(f"\n{'=' * 72}\n[{label}] $ {' '.join(cmd)}\n{'=' * 72}")
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=_ENV)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        print(out[-2500:])
    if err:
        print("STDERR:", err[-1200:], file=sys.stderr)
    if expect is not None and proc.returncode != expect:
        raise SystemExit(f"[{label}] 期望退出码 {expect}，实际 {proc.returncode}")
    return proc


def py(*args: str) -> list[str]:
    return [sys.executable, "-m", "ctxsdd", *args]


def main() -> int:
    if not os.environ.get("MINIMAX_API_KEY"):
        print("缺少 MINIMAX_API_KEY", file=sys.stderr)
        return 2

    # 0. 复位：删除状态库与上一轮记录，保证「从空状态开始」
    if STATE_DB.exists():
        STATE_DB.unlink()
    for name in ("T-001.minimax_raw.txt", "T-001.minimax_patch.py", "T-001.minimax.diff", "T-001.applied.diff", "T-001.verify.log", "powercontext_state.log", "minimax_devlog.md"):
        path = ROOT / "records" / name
        if path.exists():
            path.unlink()
    # 复原被改造的源文件（若有快照）
    baseline = ROOT / "samples" / "task-001" / "service.before.py"
    target = ROOT / "demo_project" / "taskboard" / "service.py"
    if baseline.exists():
        shutil.copyfile(baseline, target)

    checks: list[tuple[str, bool, str]] = []

    # 1. 初始化 + 导入 v1（导入前必须把 specs/ 显式切回 v1，
    #    否则上一轮遗留的 v2 会被当成 v1 导入，导致版本对比失效）
    run(py("init"), label="init")
    shutil.copyfile(ROOT / "samples/task-001/spec-v1/SPEC-001-task-filter.md",
                    ROOT / "specs/SPEC-001-task-filter.md")
    run(py("import", "--run-id", "import-v1"), label="import-v1")

    # 2. 空状态读取（应为 None）
    proc = run(py("state", "show", "--task-id", TASK_ID), expect=4, label="state-empty")
    checks.append(("从空状态开始", proc.returncode == 4, "无历史状态时返回退出码 4"))

    # 3. 上下文 v1
    run(py("context", "--task-file", "samples/task-001/task.md", "--task-id", TASK_ID,
           "--out", "samples/task-001/context_v1.json"), label="context-v1")

    # 4. 修改前测试：必须失败
    proc = run(py("verify", "--task-id", TASK_ID, "--spec-version", "v1", "--write-state"),
               expect=1, label="verify-before")
    before = json.loads(proc.stdout)
    checks.append(("修改前目标测试失败", before["exit_code"] == 1 and "failed" in before["tail"], before["tail"].splitlines()[-1]))

    # 5. 规格更新到 v2 并重导入
    shutil.copyfile(ROOT / "samples/task-001/spec-v2/SPEC-001-task-filter.md",
                    ROOT / "specs/SPEC-001-task-filter.md")
    run(py("import", "--run-id", "import-v2"), label="import-v2")
    run(py("context", "--task-file", "samples/task-001/task.md", "--task-id", TASK_ID,
           "--out", "samples/task-001/context_v2.json"), label="context-v2")

    v1 = json.loads((ROOT / "samples/task-001/context_v1.json").read_text(encoding="utf-8"))
    v2 = json.loads((ROOT / "samples/task-001/context_v2.json").read_text(encoding="utf-8"))
    v1_ids = [s["id"] for s in v1["specs"]]
    v2_ids = [s["id"] for s in v2["specs"]]
    checks.append(("规格更新后使用新内容", v2_ids != v1_ids and any("@v2" in i for i in v2_ids),
                   f"{v1_ids} -> {v2_ids}"))
    checks.append(("上下文来源真实存在", not v2["missing_sources"], f"missing={v2['missing_sources']}"))

    # 6. MiniMax 生成 + 应用
    run(py("propose", "--task-id", TASK_ID, "--task-file", "samples/task-001/task.md",
           "--source", "taskboard/service.py", "--function", "filter_tasks"), label="propose")
    run(py("apply", "--task-id", TASK_ID, "--source", "taskboard/service.py",
           "--function", "filter_tasks"), label="apply")

    # 7. 修改后测试：必须全过
    proc = run(py("verify", "--task-id", TASK_ID, "--spec-version", "v2", "--write-state"),
               expect=0, label="verify-after")
    after = json.loads(proc.stdout)
    checks.append(("修改后全部测试通过", after["exit_code"] == 0, after["tail"].splitlines()[-1]))

    # 8. 状态复用：读取最新状态
    proc = run(py("state", "show", "--task-id", TASK_ID), expect=0, label="state-show")
    state = json.loads(proc.stdout)
    ok_state = (
        state.get("spec_version") == "v2"
        and any(v.get("exit_code") == 0 for v in state.get("verifications", []))
    )
    checks.append(("重启后可读取状态并用于后续", ok_state,
                   f"spec={state.get('spec_version')} verifications={[v.get('exit_code') for v in state.get('verifications', [])]}"))

    # 9. 汇总
    print("\n" + "=" * 72)
    print("验收自检")
    print("=" * 72)
    all_ok = True
    for name, ok, detail in checks:
        flag = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"  [{flag}] {name}：{detail}")

    report = {
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        "all_ok": all_ok,
    }
    out = ROOT / "records" / "acceptance_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {out}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
