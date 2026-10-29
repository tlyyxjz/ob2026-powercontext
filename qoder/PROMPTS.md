# Qoder 赛道 · 提示词手册 v3（2026-10-29 10:2x，技术路线改判 seekdb）

> **v1 的错**：让 Qoder 用 Sites 数据库 / 自建后端做记忆，**完全没碰 seekdb 或 PowerContext**，
> 违反题面 §2.2 的硬要求，基础验收「技术结合有效」直接不成立。
>
> **v2 的错**：改成 PowerContext 之后，我在本机装不上它——`pylibseekdb` 有官方 Windows wheel，
> 而 PowerContext 的 `server` extra 硬依赖 `fastmcp`，这个包在本机装出来是空壳（只有 `dist-info`、
> 零 `.py`），`pip download` 连文件都下不出来。README 也写着 `Windows support is experimental`。
>
> **v3 的判据**：题面 §2.2 原文是「实际使用 **seekdb、PowerContext 中的至少一种**」，二选一即可。
> 选**能跑通的那个**：seekdb 嵌入式模式，`pip install` 两条命令，不起服务、不用 Docker、不用模型 Key。

---

## 一、已核实的 seekdb 事实（都有出处，不是印象）

### 1. 嵌入式运行，不用部署服务

官方 README「30-Second Try」与「Quick Start → Embedded use」原文：

> Use a local seekdb database from Python, Node.js, or Go **without deploying a database service yourself**.
> Data is stored in a local directory and remains available when you reopen it.

```bash
python3 -m pip install -U pylibseekdb pymysql
```

用法（README 原文示例的结构）：`pylibseekdb.open("./seekdb.db")` 拿到 `connection_options()`，
交给 `pymysql.connect(database="test", autocommit=True, cursorclass=DictCursor, **options)`。

- 要求 **Python 3.11+**
- 我在本机 `pip install` 解析出的 wheel 名：**`pylibseekdb-1.4.0.post2-cp312-abi3-win_amd64.whl`（65.9 MB）** ⇒ Windows 有官方构建

🔴 **两个已实测的坑（2026-10-29 11:2x 现场验过，别再踩）**：

1. **`pylibseekdb` 没有 `__version__` 属性**。`print(pylibseekdb.__version__)` 直接抛
   `AttributeError: module 'pylibseekdb' has no attribute '__version__'`。
   读版本必须用 `from importlib.metadata import version; version("pylibseekdb")`。
   它的公开属性只有：`Connection / Cursor / SeekdbError / SeekdbInstance / aopen / close /
   connect / connection_options / open / pylibseekdb`。
2. **依赖已装在固定解释器里，不要新建 venv 重新下**（65.9 MB 的 wheel 重下要 2 分钟以上，
   而且清华镜像没有这个包容易误判"装不上"）：

   ```
   C:\Users\Lenovo\WorkBuddy\_venv_review\Scripts\python.exe
   ```

   实测读数：`pylibseekdb 1.4.0.post2`｜`pymysql 2.2.8`｜`Python 3.13.14`。

**本机实测结果（2026-10-29 10:3x，Python 3.13.14，`pylibseekdb 1.4.0.post2`）**：

| 验证项 | 读数 |
|---|---|
| `SELECT 1` | `[{'value': 1}]`（与 README 预期一致） |
| 建表带 `FULLTEXT INDEX ... WITH PARSER ik` | `CREATE TABLE ok`，5 行入库 |
| 中文全文检索 `MATCH(stem) AGAINST('索引')` | 命中 **2** 行 `[1, 2]` |
| `AGAINST('回表')` | 命中 **1** 行 `[2]` |
| `AGAINST('隔离级别')` | 命中 **1** 行 `[3]` |
| `AGAINST('redo log')` | 命中 **2** 行 `[5, 4]` |
| 关库重开后 `COUNT(*)` | 仍是 **5**（数据真落盘） |

> ⚠️ 下载环境有个坑：`files.pythonhosted.org` 本机只有 **15 KB/s**，65.9 MB 要 70 分钟。
> 换国内镜像（阿里云 / 中科大 / 腾讯三家都有这个 wheel）到 **452 KB/s**，2 分 22 秒下完，
> 且 `sha256` 与 PyPI 官方索引一致（`2760ad36…66dba5`）。
> 但**清华镜像这个包没有**——三家有、一家没有，别只试一家就下结论。

### 2. 建表与检索的 SQL 规格（README 「SQL — Schema + Hybrid Search」原文）

```sql
CREATE TABLE articles (
  id        INT PRIMARY KEY,
  title     TEXT,
  content   TEXT,
  embedding VECTOR(384),
  FULLTEXT INDEX idx_fts (content) WITH PARSER ik,
  VECTOR   INDEX idx_vec (embedding) WITH (DISTANCE=l2, TYPE=hnsw, LIB=vsag)
) ORGANIZATION = HEAP;

SELECT id, title, l2_distance(embedding, '[...]') AS dist
FROM articles
WHERE MATCH(content) AGAINST('quarterly report')
ORDER BY dist APPROXIMATE
LIMIT 10;
```

两个关键点：

- **`WITH PARSER ik` 是中文分词**——我们的题库是中文题干，这个必须带上，否则检索会碎成单字
- **向量列可以不要**。不配 `embedding` 就不需要任何模型 API，评审零配置能跑。全文 + 结构化条件已经满足题面「知识库检索、内容匹配或业务数据查询」

### 3. 🔴 库目录不能进 git，只能进"初始化脚本 + JSON 数据"

实测一个**只装了 5 行数据**的库目录，占了 **328.8 MB**，构成：

| 文件 | 大小 | 性质 |
|---|---|---|
| `run/seekdb.exe` | **127.0 MB** | seekdb 自己往数据目录里拷了一份可执行文件 |
| `store/redo/sys/log_stream/meta/0` | 67.1 MB | redo log，**预分配** |
| `store/redo/sys/log_stream/log/0` | 67.1 MB | redo log，**预分配** |
| `log/seekdb.log` | 34.0 MB | 运行日志 |
| `store/sstable/block_file` | 33.6 MB | 数据文件 |

⇒ 三条硬约束：

1. **`.gitignore` 必须排除库目录**（例如 `*.db/`、`seekdb.db/`），否则仓库直接爆
2. 验证样例交的是 **JSON 初始数据 + 一个初始化脚本**，不是库文件。评审跑一次脚本就有库
3. README 里要提醒：**首次初始化会占约 330 MB 磁盘**，机器空间紧的要先看一眼

### 4. 为什么不是 PowerContext（备选保留）

PowerContext 的能力更适合「记住用户偏好/任务进展」，但它的启动路径是
`python -m powercontext.server.cli run`，而 `server/access.py:24` 硬 import `fastmcp`，
`mcp.enabled=false` 也跳不过。本机装不上 ⇒ 评审大概率也装不上。
**如果以后拿到能装的环境，可以把记忆层换回去**，产品设计不用改（都是"存历史 + 检索"）。

---

## 二、产品定位（不再改）

**复盘面试官**——专挑你上次答崩的那道题，再找一道相似的，让你重练。

- 用户：准备数据库/后端岗位面试的学生，每天 15 分钟
- 痛点：刷题软件只记"对没对"，不记"你答错在哪一句"；换个说法同一道题又崩
- seekdb 在这里干的活（对应题面的三种用途，逐条落）：
  - **知识库检索**：`MATCH(stem) AGAINST('<上次答错的题的题干关键词>')` 找出同类题
  - **内容匹配**：把你这次的原话和上次的原话比，看你是否重复同一个错
  - **业务数据查询**：按 `mastery < 60` + 难度 + 最近练习时间排今天的题单
- 为什么不是硬套：**没有 seekdb 这个功能就退化成"随机出题"**——正因为要"找相似题"才需要一个能同时做中文全文匹配和结构化过滤的引擎

---

## 三、六段提示词（同一个 Qoder 会话，逐段发，一段一次）

### P1 · 先只读，出方案（不写代码）

```
先不要写任何代码，也不要创建文件。做三件事，做完停下等我确认：

1. 环境自检：用这个解释器跑（不要新建 venv、不要重新 pip install，依赖已装好）：
   C:\Users\Lenovo\WorkBuddy\_venv_review\Scripts\python.exe

   跑这两条，把输出原样贴给我：
   ###
   from importlib.metadata import version
   print("pylibseekdb", version("pylibseekdb"))
   print("pymysql", pymysql.__version__)
   ###

   注意：pylibseekdb 这个包没有 __version__ 属性，不要写 pylibseekdb.__version__，
   那样会抛 AttributeError，看起来像"没装"，其实装了。
   真跑不起来就明确说跑不起来并贴完整报错，不要跳过。

2. 读下面这份接口规格，告诉我你打算怎么用它支撑产品功能：
   - 打开库：pylibseekdb.open(数据目录) → 拿到 connection_options() → 交给 pymysql.connect
   - 建表要带中文全文索引：FULLTEXT INDEX 名字 (列名) WITH PARSER ik
   - 检索：WHERE MATCH(列名) AGAINST('关键词')  可以和普通 WHERE 条件一起写
   - 约束：同名表重复建会报错，初始化要可重复执行（先判断存在再建）
   - 不需要向量列，也不需要任何模型 API

3. 给出文件清单（每个文件一句话职责），说明哪些是新写的。

注意：数据层只能用 seekdb，不要用 localStorage、不要另建 sqlite、不要用 Sites 数据库。
```

### P2 · 跑通数据层（产出要能当场验证）

```
现在只做数据层，不做界面。

1. 写一个 Python 模块，负责：
   - 初始化：在指定目录打开/创建 seekdb 库，建两张表（题库、作答历史），可重复执行不报错
   - 题库表的题干列要有中文全文索引（WITH PARSER ik）
   - 导入：从 JSON 读初始题库写入（幂等，重复导入不产生重复行）
   - 记录一次作答：题号、第几次、用户原话、得分、点评、时间
   - 查相似题：给定一段文本，用全文匹配找出相似题库行，并带上"我在这道题的当前得分"
   - 今日题单：按得分从低到高、跳过最近已练过的，取出 N 道

2. 写一个自检脚本，做完把输出贴给我（不要只说"已完成"）：
   a. 初始化后 SELECT COUNT(*) 的题库行数
   b. 全文检索一个中文关键词，命中的题号与标题
   c. 写两条作答记录后，某道题的当前得分
   d. 关闭程序、重新打开目录，再查一次 c 的结果（证明数据真的落盘了）
```

### P3 · 前端（先能跑，不接模型）

```
现在做前端，放 web/ 目录，纯静态，不要构建工具、不要 npm。

页面结构（单页，桌面和手机都能看）：
1. 顶部：今日目标（例如「今天 3 题，还有 2 道没过」）+「开始」
2. 中间：题目卡（题干 + 难度 + 我在这道题的当前得分）
3. 下部：回答框 + 「交卷」
4. 交卷后：点评区三块（答到的点 / 漏掉的点 / 一句话建议）+「下一题」「再答一次」
5. 右上角「错题本」抽屉：所有得分低于 60 的题，点一条可以跳回去重练
6. 左下角状态条：显示数据层连没连上、当前库里有多少条作答记录（给评审看的，不要藏）

规则：
- 交卷后必须先写库成功，再显示下一题；失败要显示「这次没存进去，别走」
- 出下一题时，优先「我得分低的」和「与上一题相似但我没练过的」
- 前端调本机后端服务（默认地址可配）

视觉：浅色、留白多、不要花哨渐变；中文字体用系统字体；按钮有 hover 和 disabled 态。
这一版点评先用固定三段话术（P4 再接模型），题库用 P2 导入的那 8–20 道。

生成后停下，告诉我打开方式和用到哪些文件。
```

### P4 · 点评接模型（可降级，没配 Key 也要能用）

```
现在把点评交给模型生成，不要用固定模板拼句子。

- 模型调用放在后端做，**不要把 API Key 放浏览器里**；配置从环境变量读：
  LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，没配就自动降级成规则点评
- 给模型的内容必须包含：题干、参考答案要点、用户这次的原话、以及**这个用户这道题之前的作答**
  （从 seekdb 里查出来的，不是猜的）
- 输出严格三段：「你答到的点：」「你漏掉的点：」「一句话建议：」，总长 ≤120 字，不要客套话
- 模型失败时仍然要写库，并显示「点评暂时不可用，这次作答已经记下来了」

做完贴一段真实输入和返回；再单独演示一次「没配 Key」的降级路径。
```

### P5 · 补空态/错误态 + 出两份验证材料

```
不改产品设定的前提下补这几件事，每改一处说明改的是哪个文件：

1. 空态：第一次进来要说清「这里会记住你答崩的题」
2. 加载态：出题和点评都要有转圈，不能点了没反应
3. 错误态：数据目录没初始化 / 库文件被占 / 模型 Key 无效，三种文案要不一样
4. 移动端：375px 宽不横向滚动
5. 无障碍：输入框有 label，按钮有 focus 样式

然后写两份文件，内容都要能别人照着重跑出同样结果：
- docs/VERIFY.md：初始数据、操作步骤、每步预期结果，以及"seekdb 真的起作用"的记录——
  要贴出实际的 SQL 与命中行数（例如 MATCH 命中了哪几行、WHERE 过滤掉几行）
- docs/development-log.md：按时间顺序列出我提的每一段需求、你改了哪些文件、遇到什么错误怎么修的
```

### P6 · 发布用站点

🔴 **先看清硬约束（2026-10-29 查 `sites describe ensure_backend` 实测）**：
Qoder Sites 的后端能力只有 `database` / `functions` / `storage` / `qoder` 四类，
**没有 Python 运行时**。我们的产品是 Python + `pylibseekdb` 嵌入式，
**不可能托管到 Sites 上**。所以网站不是"在线版产品"，只能是两样东西：

1. 项目介绍页（讲清楚解决了什么问题、seekdb 用在哪一环）
2. **检索记录回放页**——把本地 seekdb 真实执行的查询与结果导出成 JSON，页面上逐条回放

⛔ 绝对不要在前端用 JS 假装做检索（假 demo）。回放页必须标明「以下数据由本地 seekdb 实际执行后导出」。

```
现在做站点，放 web/site/ 目录，纯静态，不要构建工具。

结构：
1. 首屏：一句话说清项目（把崩过的题再问一遍），配一张产品主界面截图
2. 「seekdb 用在哪」一节：画一个三格流程图 —— 作答 → 写库(seekdb) → 下次检索同类题(seekdb)
   ，用 HTML/CSS 画，不要图片
3. 「检索记录回放」一节：从 demo/queries.json 读数据，逐条展示
   每条显示：用户当时输入的关键词、实际执行的 SQL、命中的题号与匹配分数、命中题干
   顶部固定一行小字：「以下为本地 seekdb 实际执行的查询结果，导出时间 <时间>」
4. 「怎么在本地跑起来」一节：指向 README 的步骤，写出关键命令行
5. 页脚：代码仓库链接、Qoder 账号 UID、许可证

要求：
1. 不要编造使用数据。没有真实用户就写「暂无真实用户数据」
2. 截图必须用本机实际跑出来的界面
3. 页面里不要出现任何 API Key、token、内网地址
4. 单页 + 一个 CSS，不要 npm，不要打包

生成后停下，把文件清单和本地打开方式给我。发布我单独确认后再做。
```

---

## 四、发布与提交（我执行，你不用记）

已实测 `qodercn sites` 的操作目录，关键几条：

| 操作 | 用途 | 关键约束（原文） |
|---|---|---|
| `prepare_site` | 打包上传草稿（不发布） | 内容变了要换新 actionId |
| `publish_site` | 上线 | 复用 actionId 重试 |
| `get_publish_status` | 查结果 | `readiness=publication_completed` 才算真上线 |
| `update_access_policy` | 设公开 | 「Public sharing requires explicit authorization and **confirmPublic=true**」 |
| `get_analytics` | 取 PV/UV | 24h/7d/30d；UV 用服务端总数，不能自己加总 |

提交材料（题面 §4 逐字）：项目网站网址、代码仓库、运行说明、验证样例、Qoder 开发记录（含账号 UID）。

---

## 五、四条别踩

1. **不要贴现成代码给 Qoder** —— 需求与接口规格可以贴，可执行代码不行。
2. **数据层不要出现第二个存储** —— 只要 seekdb，别混 sqlite / localStorage / Sites 数据库。
3. **不要先发布再验证** —— 公开链接改一次换一次。
4. **中文全文索引必须带 `WITH PARSER ik`** —— 不带的话中文检索会碎成单字，功能看着能跑但结果全是错的。
