# records/ 说明：这些记录是什么、对应哪个代码版本

本目录是 `python scripts/run_acceptance.py` 在**真实运行**中写出的证据，不是手写的说明。
题面要求「测试日志需记录命令、退出码和对应代码版本」，这里补上第三条。

## 1. 每份文件是什么

| 文件 | 内容 | 由谁写 |
|---|---|---|
| `T-001.minimax_raw.txt` | MiniMax 原始返回（只截取函数体，不含思维链） | `ctxsdd propose` |
| `T-001.minimax.diff` | 从原始返回里抽出的代码，与修改前快照的差异 | `ctxsdd propose` |
| `T-001.minimax_patch.py` | 同上，纯代码形式，便于对照 | `ctxsdd propose` |
| `T-001.applied.diff` | **实际落盘**的修改与修改前快照的差异 | `ctxsdd apply` |
| `T-001.verify.log` | 测试命令、工作目录、`exit_code`、pytest 结果行 | `ctxsdd verify` |
| `powercontext_state.log` | PowerContext 的写入/读回记录（含 revision） | `ctxsdd state` |
| `minimax_devlog.md` | 一次任务的完整链路：上下文 → 请求 → 返回 → 落地 → 验证 | `run_acceptance.py` |
| `acceptance_report.json` | 六项必做自检的逐项结论与读数 | `run_acceptance.py` |

## 2. 对应哪个代码版本

- 这批记录生成于 **2026-10-28** 的一次完整验收运行。
- 那次运行之后，`minimax/` 下被改动的文件只有三个：`README.md`、`scripts/run_acceptance.py` 的模块
  docstring、`一页技术说明.md`。**`ctxsdd/`、`demo_project/`、`specs/`、`samples/`、`records/` 一个字节都没动**，
  所以上面这些读数对应的就是当前提交里的代码。
- 复核方法（在你的克隆里执行，输出应为空）：

```bash
git log --oneline -1
git status --porcelain
```

- 平台侧以「平台记录的固定提交」（commit hash）为准；本仓库不另行标注版本号。

## 3. 一条已知的口径

`T-001.verify.log` 里那条命令是**开发机上的绝对路径**（`C:\Users\...\ob2026-minimax\.venv\Scripts\python.exe`），
它对应仓库内的相对路径 `minimax/demo_project`（见 `# cwd:` 那一行）。换机器复现请以 `README.md` 第 5 节的
一键验收为准——那条命令在任何机器上都成立。
