# MiniMax × OceanBase：规有所依 —— 面向软件持续演进的 SDD 上下文管理系统

本目录是「MiniMax」赛道（20 分）的参赛作品。它实现了一个**规格驱动开发（Spec-Driven
Development, SDD）的工程上下文模块**，并在一项真实的局部代码修改上跑通了完整闭环。

一句话概括：

> 给定一个开发任务，模块从 **seekdb** 中检索出「该遵守的规格条目、该改的代码符号、该跑的测试」，
> 把这份上下文交给 **MiniMax** 生成代码修改；修改前后的测试结果、任务进展由 **PowerContext**
> 持久化，模块重启后仍能读取并继续使用。

---

## 1. 目录结构

```
minimax/
├── README.md                     本文件（运行说明）
├── 一页技术说明.md                模块结构 / 上下文选择方式 / 技术分工
├── requirements.txt              第三方依赖
├── config.example.env            配置示例（全部为占位值）
├── ctxsdd/                       上下文模块（核心交付）
│   ├── cli.py                    调用入口（CLI，6 个子命令）
│   ├── config.py                 配置（环境变量驱动）
│   ├── store.py                  seekdb 存储与检索
│   ├── importer.py               规格 / 代码符号 / 测试用例解析入库
│   ├── state.py                  PowerContext 任务状态读写
│   ├── minimax_client.py         MiniMax HTTP API 客户端
│   └── agent.py                  上下文 → 提示词 → 代码修改 → 落地
├── demo_project/                 业务工程（被改造的对象）
│   ├── taskboard/                领域模型 + 查询服务
│   ├── tests/                    回归测试 + 目标功能测试
│   └── pyproject.toml
├── specs/                        当前生效的规格
├── samples/task-001/             验证样例（任务描述、两版规格、改前快照、两版上下文）
├── records/                      技术使用记录（MiniMax 记录、SQL 记录、状态记录、测试日志）
└── scripts/run_acceptance.py     一键验收脚本（六项自检）
```

---

## 2. 依赖与环境

| 组件 | 版本要求 | 说明 |
|---|---|---|
| Python | ≥ 3.11 | 开发与验证使用 3.13 |
| PowerContext | `oceanbase/powercontext` 的 `2026-competition` 分支 | 该分支带 `pyproject.toml` 与 `builtin` extra，见 2.2 |
| seekdb | `oceanbase/seekdb` 镜像，实测 `seekdb-v1.4.0.0` | 走 MySQL 协议，默认 `127.0.0.1:2881` |
| MiniMax | `MiniMax-M3.1-Flash`（OpenAI 兼容 HTTP API） | 模型名可通过 `MINIMAX_MODEL` 覆盖 |
| pymysql | ≥ 1.1 | 连接 seekdb |

### 2.1 启动 seekdb

```bash
docker run -d --name seekdb -p 2881:2881 oceanbase/seekdb:latest
# 等待约 30 秒初始化，验证连接：
mysql -h127.0.0.1 -P2881 -uroot -e "SELECT VERSION()"
```

期望输出包含 `5.7.25-OceanBase seekdb-v1.4.0.0`。

### 2.2 安装 PowerContext

PowerContext 的源码**已随本目录交付**在 `vendor/powercontext/`（Apache-2.0，
来源分支与 commit 见 `vendor/NOTICE.md`，未修改任何文件）。因此评审**不需要再联网
clone 任何仓库**——赛道仓库的 `2026-competition-ecosystem` 分支本身不含 PowerContext
源码，也没有 `pyproject.toml`。

```bash
# 在 minimax/ 目录下
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` 已列出 PowerContext 运行所需的依赖；`vendor/` 通过第 3 节的
`PYTHONPATH` 直接引用。

> 备选方式（效果相同）：如果更希望用 pip 管理这个依赖，可以
> `git clone -b 2026-competition https://github.com/oceanbase/powercontext.git`，
> 然后在该仓库根执行 `pip install -e ".[builtin]"`。

> 本作品只使用 PowerContext 的公开 SDK 接口（`open_builtin_contexts` +
> `BuiltinArtifacts.memory`），未修改其任何源码。

### 2.3 配置

```bash
# 必填：MiniMax API Key（仓库内不保存任何真实凭据）
export MINIMAX_API_KEY='sk-...'          # Windows PowerShell: $env:MINIMAX_API_KEY='sk-...'

# 可选：覆盖默认值
export SEEKDB_HOST=127.0.0.1
export SEEKDB_PORT=2881
export SEEKDB_USER=root
export SEEKDB_PASSWORD=''
export SEEKDB_DATABASE=minimax_ctx
export MINIMAX_MODEL=MiniMax-M3.1-Flash
export CTXSDD_STATE_DB=./var/task_state.db
```

全部可配置项的默认值见 `ctxsdd/config.py`。

---

## 3. 调用入口

所有命令在 `minimax/` 目录下执行：

```bash
cd minimax
export PYTHONPATH="$PWD:$PWD/vendor"     # Windows 用分号分隔
```

> 上面这一行已经包含 `vendor/`（随本目录交付的 PowerContext 源码），**默认情况下不需要
> 再设置任何别的东西**。只有改用 2.2 节的备选方式（`pip install -e` 装了源码仓库）时，
> 才需要把 `$PWD/vendor` 换成那个仓库的 `src` 目录，或直接用 `POWERCONTEXT_SRC` 指定。

| 命令 | 作用 |
|---|---|
| `python -m ctxsdd init` | 重建 seekdb 资料库（5 张表） |
| `python -m ctxsdd import --run-id import-v1` | 解析 `specs/` 与 `demo_project/`，写入 seekdb |
| `python -m ctxsdd context --task-file samples/task-001/task.md --task-id T-001 --out ctx.json` | 输出任务上下文（JSON + 人类可读摘要） |
| `python -m ctxsdd state save --task-id T-001 --done "..." --todo "..."` | 保存任务状态（PowerContext） |
| `python -m ctxsdd state show --task-id T-001` | 读取任务状态（新进程即可读回） |
| `python -m ctxsdd propose --task-id T-001 --task-file ... --source taskboard/service.py --function filter_tasks` | 调用 MiniMax 生成修改 |
| `python -m ctxsdd apply --task-id T-001 --source taskboard/service.py --function filter_tasks` | 应用上一步生成的修改 |
| `python -m ctxsdd verify --task-id T-001 --write-state` | 运行测试并记录结果 |

### 3.1 输入与输出

**`context` 的输出**（`--out` 写入 JSON，同时打印摘要）：

```jsonc
{
  "task": "原始任务描述",
  "keywords": ["taskboard", "filter_tasks", "标签", ...],
  "specs":   [{"id": "SPEC-001#R2@v2", "title": "标签筛选", "source_path": "specs/SPEC-001-task-filter.md", "body": "..."}],
  "symbols": [{"id": "taskboard/service.py::filter_tasks", "path": "taskboard/service.py", "symbol": "filter_tasks", "start_line": 13, "end_line": 50, "signature": "..."}],
  "tests":   [{"id": "tests/test_filter_by_tag.py::test_filters_by_tag", "path": "tests/test_filter_by_tag.py", "name": "test_filters_by_tag", "covers": "SPEC-001#R2"}],
  "state":   {"spec_version": "v2", "verifications": [{"exit_code": 1}, {"exit_code": 0}], "_revision": 2},
  "sources": {"specs": [...], "code": [...], "tests": [...]},
  "missing_sources": []          // 非空即代表有来源不真实存在
}
```

`missing_sources` 是**内置的来源真实性校验**：模块会逐个检查输出里引用的文件是否存在，
不存在就返回非 0 退出码。上面的样例输出中该项为空列表。

---

## 4. 数据初始化与验证样例

验证样例在 `samples/task-001/`：

| 文件 | 作用 |
|---|---|
| `task.md` | 任务描述：要改什么、哪些原有行为必须保持、验收方式 |
| `spec-v1/SPEC-001-task-filter.md` | 规格 v1（5 条）—— 任务开始时生效 |
| `spec-v2/SPEC-001-task-filter.md` | 规格 v2（6 条）—— 任务过程中更新（R2 改为不区分大小写，新增 R6） |
| `service.before.py` | 修改前的源码快照 |
| `context_v1.json` | 基于 v1 规格的上下文 |
| `context_v2.json` | 基于 v2 规格的上下文（用于核对「规格更新后使用新内容」） |

**重置方法**（回到「未改造」状态，可重复验收）：

```bash
cp samples/task-001/service.before.py demo_project/taskboard/service.py
cp samples/task-001/spec-v1/SPEC-001-task-filter.md specs/
rm -f var/task_state.db records/T-001.* records/powercontext_state.log records/minimax_devlog.md
```

---

## 5. 一键验收

```bash
export MINIMAX_API_KEY='sk-...'
python scripts/run_acceptance.py
```

> 脚本会自动把本目录的 `vendor/` 加进子进程的 `PYTHONPATH`，所以**不需要先手工设置
> `PYTHONPATH`**。如果你改用 2.2 节的备选方式（`pip install -e` 装了源码仓库），
> 脚本同样能跑；也可以用 `POWERCONTEXT_SRC=/path/to/powercontext/src` 显式指定。

脚本会重置状态、重建资料库，依次执行「初始化 → 导入 v1 → 空状态读取 → 上下文 v1 →
改前测试 → 规格更新重导入 → 上下文 v2 → MiniMax 生成 → 应用 → 改后测试 → 重启读状态」，
最后输出六项自检结果并写入 `records/acceptance_report.json`。

实测输出（2026-10-28）：

```
[PASS] 从空状态开始：无历史状态时返回退出码 4
[PASS] 修改前目标测试失败：5 failed, 9 passed
[PASS] 规格更新后使用新内容：@v1（5 条） -> @v2（6 条，新增 R6）
[PASS] 上下文来源真实存在：missing=[]
[PASS] 修改后全部测试通过：14 passed
[PASS] 重启后可读取状态并用于后续：spec=v2 verifications=[1, 0]
```

---

## 6. 测试命令

业务工程自带两套测试，修改前后执行的是**同一份测试集**：

```bash
cd demo_project
python -m pytest -q                          # 全部 14 项
python -m pytest -q tests/test_service.py    # 回归 9 项（改前改后均须全过）
python -m pytest -q tests/test_filter_by_tag.py  # 目标功能 5 项（改前失败、改后通过）
```

修改前的实测结果：`5 failed, 9 passed`（全部是 `AssertionError`，非导入/启动错误）。
修改后：`14 passed`。

日志见 `records/T-001.verify.log`（含完整命令、工作目录与退出码）。

---

## 7. 技术使用记录

| 文件 | 内容 |
|---|---|
| `records/minimax_devlog.md` | 每次 MiniMax 调用的时间、模型、耗时、token、提供的上下文摘要、模型输出原文 |
| `records/T-001.minimax_raw.txt` | 本次任务 MiniMax 的完整原始响应 |
| `records/T-001.minimax_patch.py` / `.diff` | 从响应中提取出的补丁代码与差异 |
| `records/T-001.applied.diff` | 实际落地到 `demo_project` 的差异 |
| `records/powercontext_state.log` | 每次任务状态保存/读取的记录（含 revision） |
| `records/T-001.verify.log` | 测试命令、退出码与输出 |
| seekdb `query_log` 表 | 每次上下文检索实际执行的 SQL、参数与命中行数 |

> 说明：`records/` 里记录的命令与工作目录是**作品开发机上的绝对路径**（Windows），
> 与仓库内的相对路径一一对应（例如 `…\minimax\demo_project` ↔ `minimax/demo_project`）。
> 换一台机器复现时以第 5 节的一键验收为准，它会在本目录下重新生成这套记录。

查看数据库查询记录：

```bash
mysql -h127.0.0.1 -P2881 -uroot minimax_ctx -e \
  "SELECT purpose, row_count, LEFT(statement,80) AS sql, logged_at FROM query_log ORDER BY seq DESC LIMIT 10"
```

---

## 8. 已知限制

1. **seekdb 必须先起**。本模块不使用 SQLite 兜底——题面要求 seekdb 承担实际检索，
   降级会违反「仅安装组件不满足要求」。seekdb 不可用时 `init/import/context` 直接报错退出。
2. **MiniMax 输出未被缓存**。`propose` 每次都会真实调用模型，因此产出的代码措辞可能不同
   （题面明确「不要求模型重新生成完全相同的补丁」）。已经生成的补丁保存在 `records/` 中，
   `apply` 只认 `records/T-001.minimax_patch.py` 这一份，评审可离线复现修改结果。
3. **代码符号解析只覆盖 Python**。`importer.py` 用标准库 `ast` 实现，换了语言需替换解析器。
4. **上下文检索是关键词 OR 匹配 + 打分排序**，未使用向量检索。选择依据与局限见
   `一页技术说明.md` 第 3 节。
5. **`init` 是破坏性的**：会删除并重建 `minimax_ctx` 库中的 5 张表。状态库
   （`var/task_state.db`）不受影响。
6. `var/task_state.db` 是运行产物，不入库；按第 4 节的重置步骤可从空状态重建。
