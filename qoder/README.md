# 复盘面试官 · 专挑你答崩的那道题

一个本地运行的面试复盘工具。刷题软件只记「对没对」，它记**你答错在哪一句**，
然后从题库里找出**同类题**让你重练 —— 「找同类题」这一步由 **seekdb 的中文全文检索**完成，
没有 seekdb 这个功能就退化成随机出题。

- 技术栈：Python 标准库 `http.server` + `pylibseekdb`（seekdb 嵌入式）+ 原生 HTML/CSS/JS
- 无构建工具、无 npm、无 CDN、不需要 GPU
- 数据全部落在本地一个 seekdb 目录里，**不上传任何服务器**

---

## 一、运行环境

| 项 | 版本（本机实测） | 说明 |
|---|---|---|
| Python | **3.13.14** | 需 **3.11+**（`pylibseekdb` 的 wheel 是 cp312-abi3，3.11 起可用） |
| pylibseekdb | **1.4.0.post2** | Windows 有官方 wheel：`pylibseekdb-1.4.0.post2-cp312-abi3-win_amd64.whl`（65.9 MB） |
| pymysql | **2.2.8** | 只用它作为 seekdb 的 DB-API 客户端 |
| 操作系统 | Windows 11 | Linux/macOS 同样有 wheel，命令里的路径按平台改 |
| 磁盘 | 可用 ≥ **400 MB** | 首次初始化后 `.data/seekdb.db` 约 **330 MB**（见第六节） |
| 端口 | 8765（可改） | 只监听 `127.0.0.1`，不对外网开放 |

seekdb 服务端版本（运行时读取）：`5.7.25-OceanBase seekdb-v1.4.0.0`

---

## 二、从零启动（5 步）

```bash
# 1. 建虚拟环境
python -m venv .venv

# 2. 激活（Windows Git Bash / PowerShell / CMD 任选其一）
source .venv/Scripts/activate        # Git Bash
# .venv\Scripts\Activate.ps1         # PowerShell
# .venv\Scripts\activate.bat         # CMD

# 3. 装依赖（两个包，总下载约 66 MB）
pip install pylibseekdb==1.4.0.post2 pymysql==2.2.8

# 4. 初始化数据库：建库、建表、导入题库（幂等，可以反复执行）
python db/init.py

# 5. 启动服务
python server/app.py
```

看到这样就成功了：

```
seekdb  : <仓库目录>/.data/seekdb.db
stats   : {'questions': 21, 'ref_points': 85, 'answers': 0}
listening http://127.0.0.1:8765  (web root: .../web)
```

浏览器打开 <http://127.0.0.1:8765> 即可。

**下载慢的话**：`files.pythonhosted.org` 在国内实测只有 15 KB/s，65.9 MB 要 70 分钟。
换国内镜像 2 分半能下完：

```bash
pip install pylibseekdb==1.4.0.post2 pymysql==2.2.8 \
  -i https://mirrors.aliyun.com/pypi/simple/     # 或中科大 / 腾讯
```

⚠️ 清华镜像**没有**这个包（`from versions: none`），别只试一家就以为下不了。

---

## 三、seekdb 是怎么接进去的（不是"装了个组件"）

用**嵌入式模式**：不需要起数据库服务、不需要 Docker、不需要任何模型 API。

```python
import pylibseekdb, pymysql
opts = pylibseekdb.open("./.data/seekdb.db").connection_options()
conn = pymysql.connect(database="test", autocommit=True, **opts)
```

`open()` 每次分配**动态端口**，所以 `connection_options()` 必须在 `open()` 之后从实例上取，
**不能缓存、不能写死**。

### 三张表

| 表 | 作用 | 关键设计 |
|---|---|---|
| `questions` | 题库（21 题） | `keywords` 列建 `FULLTEXT INDEX idx_q_ft (keywords) WITH PARSER ik` |
| `ref_point` | 参考答案要点（85 条，一题多行） | `content` 列建 `FULLTEXT INDEX idx_rp_ft (content) WITH PARSER ik` |
| `answer_log` | 作答历史 | 复合主键 `(question_id, attempt_no)`，重复提交不长新行 |

全部 `ORGANIZATION = HEAP`。

### seekdb 具体干了三件活（对应赛道要求的三种用途）

1. **知识库检索** —— 「找同类题」：
   ```sql
   SELECT id, MATCH(keywords) AGAINST('回表 覆盖索引') AS rel
   FROM questions
   WHERE MATCH(keywords) AGAINST('回表 覆盖索引') AND id <> 2
   ORDER BY rel DESC
   ```
2. **内容匹配** —— 「这句答到哪个要点」：拿用户原话对 `ref_point.content` 做 `MATCH ... AGAINST`，
   命中的算「答到的点」，没命中的算「漏掉的点」。
3. **业务数据查询** —— 今日题单与得分趋势：`answer_log` 左连「每题最新一次得分」，
   按得分升序、跳过最近 24 小时练过的。

---

## 四、一个必须知道的分词事实（影响检索质量）

seekdb 的 `WITH PARSER ik` 中文分词**词库里没有「回表」这类技术词**，会把它切成单字「表」。
实测证据：`AGAINST('回表')` 和 `AGAINST('表')` 返回的分数**逐位相同**
（题 2 = `1.8681573701991185`，题 10 = `1.191390926655287`）。

后果：搜「回表」会连带召回「分库分**表**」的题，只是分数低（4.52 vs 1.19）。

**本项目怎么处理**：

1. 检索只用 `keywords` 列（人工维护的术语串），不拿题干当检索字段 —— 题干里有大量
   「查询」「表」这类天然重复字，噪声不可控
2. 召回后做**相对阈值剪枝**：只保留 `score >= top * 0.3` 的行（`db/store.py` 的
   `SIMILAR_KEEP_RATIO`）。0.3 这个数是拿 21 道题逐题跑出来定的，见 `docs/VERIFY.md`
3. `AGAINST(... IN BOOLEAN MODE)` **不可用** —— 实测静默返回 0 行、不报错，不要指望它精确匹配
4. 双引号短语（`AGAINST('"回表"')`）同样无效，结果与不加引号完全一致

另外，MySQL 系全文检索有个「出现在超过 50% 行里的词会被当停用词忽略」的行为。
`db/check.py` 里带了一条词覆盖率检查：初始化后计算每个检索词的行覆盖率，**> 50% 会打印警告**，
防止以后扩题库时检索静默失效。

---

## 五、目录结构

```
qoder/
├── data/questions.json      题库（21 题 / 85 要点），唯一的数据源
├── db/
│   ├── schema.sql           三张表 DDL，含两处 ik 全文索引
│   ├── init.py              建表 + 导入，幂等（ON DUPLICATE KEY UPDATE）
│   ├── store.py             数据层唯一出口：record_answer / current_score /
│   │                        find_similar / match_ref_points / daily_plan / stats
│   └── check.py             数据层自检（含持久化证明与词覆盖率警告）
├── server/
│   ├── app.py               标准库 http.server，5 个 /api/* + 静态托管 web/
│   ├── review.py            点评（走模型，可降级为规则）
│   └── selftest.py          接口自测：14 个请求 + 4 条产品断言
├── web/                     前端单页（无构建工具）
├── demo/queries.json        站点回放用的真实查询记录（由本地 seekdb 实际执行后导出）
├── docs/
│   ├── VERIFY.md            验证样例：初始数据 / 步骤 / 预期结果 / 真实读数
│   └── development-log.md   开发记录
└── .data/                   运行时生成，已被 .gitignore 排除，不进仓库
```

---

## 六、磁盘占用（第一次跑之前先看这条）

`.data/seekdb.db` 一个**只装 21 道题**的库，实测占 **330 MB 左右**。构成：

| 文件 | 大小 | 性质 |
|---|---|---|
| `run/seekdb.exe` | 127 MB | seekdb 往数据目录里拷了一份可执行文件 |
| `store/redo/sys/log_stream/meta/0` | 67 MB | redo log，预分配 |
| `store/redo/sys/log_stream/log/0` | 67 MB | redo log，预分配 |
| `log/seekdb.log` | 34 MB | 运行日志 |
| `store/sstable/block_file` | 34 MB | 数据文件 |

所以：**库目录绝不能进 git**（`.gitignore` 已排 `.data/`、`*.db/`）。
交付的是 `data/questions.json` + `db/init.py`，评审跑一次 `init.py` 就有库。

---

## 七、接模型点评（可选）

不配模型也能用 —— 点评会降级成规则版（用 `ref_point` 的命中/未命中直接拼三段话）。

要接模型，设三个环境变量（**不要写进任何文件**）：

```bash
export LLM_BASE_URL=https://api.deepseek.com     # 兼容 OpenAI 协议的服务都行
export LLM_API_KEY=<你的 Key>                     # 占位，不提交真实值
export LLM_MODEL=deepseek-chat
```

超时 8 秒；模型失败时**仍然会写库**，前端显示「点评暂时不可用，这次作答已经记下来了」。

---

## 八、常见问题

**Q：`pip install` 报 `No matching distribution found for pylibseekdb`**
换镜像源，见第二节。清华源没有这个包。

**Q：`import pylibseekdb` 之后 `pylibseekdb.__version__` 报 AttributeError**
这个包没有 `__version__` 属性。用 `from importlib.metadata import version; version("pylibseekdb")`。

**Q：启动报端口占用**
`python server/app.py --port 8790`

**Q：想换个位置放数据**
`python server/app.py --db-dir /path/to/db`，`db/init.py` 也支持同样参数。

**Q：检索结果看着不准**
先看第四节。这不是 bug，是 ik 分词对技术词退化成单字导致的，本项目用 `keywords` 列 +
0.3 相对阈值处理。

---

## 九、开源说明

本项目在 OceanBase 2026 比赛期间完成。使用到的第三方组件：

- `pylibseekdb` / seekdb：OceanBase 开源，Apache-2.0
- `pymysql`：MIT

前端与后端均为本项目新写，没有复制其他项目的代码。

## 十、许可证

Apache-2.0（与所在仓库一致）。
