# 记仇客栈 · Dify × PowerContext

> Just for Fun —— 一个会记住你干过什么的客栈掌柜。
> 你在「半盏楼」做的每件事都会写进 PowerContext，下次换新会话进门，柳三娘照样提得出来。

---

## 1. 这个东西是什么

**玩法**：你是一个过路客，进店先报个名号。掌柜柳三娘会翻你的旧账——账上有砸店、赖账、吹牛、帮忙，她就换一套说法对付你。

- 头一回登门 → 客客气气，但打探你来路。
- 上次砸过店 → 阴阳怪气，把那次的事点名说出来，开价翻倍。
- 上次帮过忙 → 热情，白送一碟花生米，念叨「上回多亏你」。
- 上次吹过牛 → 拿你的原话堵你。

**为什么好玩**：普通聊天机器人换一次会话就失忆，这个不会。你甚至会想故意砸一次店，看她下次什么脸色。

**体验方式**：在 Dify 里跑这个 Chatflow，直接对话即可，不需要任何前端。

### Dify 与 PowerContext 的分工

| 组件 | 承担什么 | 在哪能核对 |
|---|---|---|
| **Dify** | 整个流程编排：接玩家输入 → 取记忆 → 调模型生成台词 → 写回记忆 → 输出 | Dify 的「日志与标注」里每条记录都能看到 5 个节点的输入输出与耗时 |
| **PowerContext** | 唯一的记忆载体：每个名号一个 Scope（`inn/{名号}`），事件以 Memory artifact 的 append 方式写入，跨进程、跨会话持久化 | 配套后端的 `records/memory_api.log` 逐条记录了 recall/remember 的 revision、条数与内容；`GET /history` 可导出原始条目 |

选择 PowerContext 而不是 seekdb，依据题面 2.2：「选择 PowerContext 的作品不额外要求使用 seekdb」。seekdb 在本作品里没有承担职责，因此**没有**声称使用它。

> 题面允许「通过 HTTP API、自定义工具或配套后端完成接入」，本作品用的是**配套后端**：`dify/backend/memory_api.py` 把 PowerContext 的 Memory artifact 包成 4 个 HTTP 端点，供 Dify 的 HTTP Request 节点调用。后端不含业务逻辑（谁该说什么全由 Dify 的 LLM 节点决定）。

---

## 2. 目录与文件用途

```
dify/
├── dsl/
│   └── 01_记仇客栈.yml        ← 唯一的 DSL，直接导入即可（导入顺序：只有这一个）
├── backend/
│   ├── memory_api.py          ← 配套后端（PowerContext 记忆服务）
│   ├── requirements.txt       ← 后端依赖（版本已锁）
│   └── README.md              ← 后端自己的启动说明
├── docs/
│   └── VERIFY.md              ← 验证样例（初始条件 / 输入 / 步骤 / 预期 / 实测）
├── scripts/
│   ├── check_dsl.py           ← DSL 体检：结构层（离线）+ 请求契约层（需后端在跑）
│   ├── verify_dify.py         ← 在 Dify 里导入 + 发布 + 跑一次，逐节点打印状态
│   └── mock_llm.py            ← 本地假模型（无 Key 时用于验证链路，不是真模型）
├── records/
│   ├── memory_api.log         ← 实际运行产生的记忆读写日志（证据）
│   └── verify_run.txt         ← verify_dify.py 一次完整运行的原样 stdout（证据）
└── data/
    └── memory.db              ← PowerContext 的 SQLite 库，**不要提交**，由后端自动创建
```

---

## 3. 从零跑起来

### 3.1 需要的环境

| 项 | 版本 | 说明 |
|---|---|---|
| Dify | **1.18.0**（docker compose 部署） | 已在本机 1.18.0 实例上导入并跑通全部 5 个节点（读数见 `docs/VERIFY.md`「Dify 侧」） |
| Python | 3.11 及以上 | 跑配套后端 |
| powercontext | **1.2.0**（`pip install "powercontext[builtin]==1.2.0"`） | 与比赛 `2026-competition` 的冻结版本同代 |
| 模型服务 | 任意 OpenAI 兼容接口 | 本文以 MiniMax 为例；换模型只需改 LLM 节点的 provider/model |

资源：Dify 全套约需 4 GB 内存、8 GB 磁盘；后端本身不到 200 MB。

### 3.2 启动配套后端

```bash
cd dify/backend
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn memory_api:app --host 0.0.0.0 --port 8765
```

起来后先自检：

```bash
curl -s http://127.0.0.1:8765/health
# {"status":"ok","db_url":"...","db_exists":true,"probe_entries":0,"error":null,...}
```

`status` 必须是 `ok`。如果不是，先看 `error` 字段，别急着往下走。

> `--host 0.0.0.0` 是必须的：Dify 跑在容器里，用 `127.0.0.1` 它连不到宿主机。

### 3.3 导入 DSL

1. Dify 控制台 →「工作室」→「创建空白应用」→ 选 **Chatflow**（不要选 Workflow，本 DSL 是 `advanced-chat`）。
   也可以直接在「导入 DSL 文件」里选 `dsl/01_记仇客栈.yml`。
2. 导入后打开应用，进「编排」页，确认 5 个节点都在：开始 → 想起旧账 → 掌柜的嘴 → 记下这笔账 → 掌柜开口。
3. 检查两处配置：
   - **环境变量** `MEMORY_API_BASE`（在右上角「环境变量」里）：默认 `http://host.docker.internal:8765`。
     - Docker Desktop（Windows/Mac）用这个默认值即可。
     - Linux 上 Docker 需要 `host.docker.internal` 映射，或者直接改成宿主机的内网 IP，例如 `http://192.168.1.10:8765`。
   - **模型**：点开「掌柜的嘴」节点 → 选择你自己的模型。
     本文用的配置是：provider `OpenAI-API-compatible`，base_url `https://api.minimaxi.com/v1`，model `MiniMax-M3.1-Flash`，temperature `0.85`。
     > 换成别的模型不影响流程，只要支持 chat 补全。

### 3.3.1 两个必做的前置（不做就会卡住）

**① 放开 SSRF 白名单**：Dify 默认禁止 HTTP 节点访问私有网段，而配套后端就在宿主机上。

```bash
# docker/.env
SSRF_PROXY_ALLOW_PRIVATE_IPS=172.16.0.0/12,192.168.0.0/16,10.0.0.0/8
docker compose up -d ssrf_proxy        # 白名单由 ssrf_proxy 的 entrypoint 写进 squid，必须重建
```

不放开时的报错原文：`Access to 'http://host.docker.internal:8765/recall_form' was blocked by SSRF protection`。

**② 模型供应商是插件，镜像里不自带**：Dify 1.x 的「模型供应商」要单独装。
在「设置 → 模型供应商」里搜 `OpenAI-API-compatible` 安装并填 `endpoint_url` + `api_key` 即可。
> 没有可用 Key 时，可以用 `scripts/mock_llm.py` 起一个本地的 OpenAI 兼容服务（默认 `http://127.0.0.1:8766/v1`），
> 它只按账本内容返回固定话术——**只用来验证链路，不冒充模型效果**。

### 3.4 验证连通（导入后先做这一步）

先跑一次仓库自带的体检脚本（它会同时查结构与请求体格式）：

```bash
pip install "pyyaml>=6"
python scripts/check_dsl.py --dry-run   # 只查结构：5 节点 4 边、无悬挂边、变量引用都指向真实节点
python scripts/check_dsl.py             # 再按 DSL 里写死的 url/form 真发一次请求（需 3.2 的后端在跑）
```

期望结尾是 `✓ 结构层通过，请求契约层通过`。

也可以一条命令跑完整链路（导入 + 发布 + 跑一次 + 逐节点打印）：

```bash
python scripts/verify_dify.py
# 期望每个节点都是 succeeded，最后一行是 workflow: succeeded | answer: ...
```

然后在 Dify 里点开「想起旧账」节点，点「运行此节点」，输入 `guest_name = 测试`，应返回：

```json
{"user_id":"测试","scope":"inn/测试","count":0,"events":[],"has_memory":false}
```

`count: 0` 且 HTTP 200 就说明 Dify 能连到后端。如果报连接拒绝，回到 3.2 检查 `--host 0.0.0.0` 与 `MEMORY_API_BASE`。

### 3.5 重置到初始状态

复现验证样例前后都需要清空某个名号的记忆：

```bash
curl -s -X POST http://127.0.0.1:8765/reset -H "Content-Type: application/json" -d '{"user_id":"测试"}'
# {"ok":true,"note":"已插入重置标记，之后 /recall 只看新发生的事",...}
```

重置是**追加一条 `reset` 标记**（而不是删数据），符合 PowerContext 的 append-only 语义；之后 `/recall` 只返回标记之后的事件。

---

## 4. 配置与凭据

- **本仓库不含任何真实 API Key。** DSL 里模型节点的密钥由 Dify 自己在「设置 → 模型供应商」里保存，不在 DSL 文件中。
- 需要你自己配置的外部服务只有一个：模型服务（见 3.3）。
- 后端不需要任何外部凭据，数据落在本地 SQLite。
- 网络条件：Dify 容器需要能访问宿主机 8765 端口；模型服务需要能出网。

---

## 5. 已知限制

1. **记忆是按「名号」索引的**，不是按 Dify 的会话 ID。换个名号就等于换个人，这是刻意的设计（便于验证"同名跨会话仍记得"）。
2. **不做语义检索**：`/recall` 按写入顺序返回该名号最近的 20 条事件，全部塞进 prompt。事件量大了（几百条以上）会撑爆上下文，需要改成摘要或向量召回——当前样例量级完全够用。
3. **单机单写**：SQLite + 单进程 uvicorn。并发几十路以上要换 PostgreSQL 后端（PowerContext 的 `SQLiteConfig` 换掉即可）。
4. 掌柜的反应依赖模型本身；换小模型可能会把「提旧账」写得含糊，这是模型能力问题，不是记忆没取到——用 `/history` 能确认记忆确实在。

---

## 6. 复用与来源

- **DSL 结构参考**：[`dzh-gitcode/dify-workflows`](https://github.com/dzh-gitcode/dify-workflows) 的 `02_图像绘画多模态/免费即梦文生视频.yml`（DSL 0.1.5 的真实导出件），只借鉴了节点与边的书写格式，**节点内容、提示词、流程全部为本作品新写**。
- **PowerContext**：`oceanbase/powercontext`，Apache-2.0，通过 PyPI 发行版 `powercontext[builtin]==1.2.0` 使用，未修改其源码。
- **Dify**：`langgenius/dify` 1.18.0，未修改，仅以其 docker compose 部署。
