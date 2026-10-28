# 验证样例（VERIFY）

> 复现前提：按 `README.md` 第 3 节把后端跑起来（`/health` 返回 `status: ok`）、把
> `dsl/01_记仇客栈.yml` 导入 Dify、并在「掌柜的嘴」节点选好模型。
> 下面三组样例按顺序做，**每组开始前都要先 `/reset`**，否则会被上一组的事件污染。

快速重置（三组都用这个名字，方便对照）：

```bash
curl -s -X POST http://127.0.0.1:8765/reset \
  -H "Content-Type: application/json" -d '{"user_id":"阿七"}'
```

预期返回 `{"ok":true,"note":"已插入重置标记...","entries_total":N,"revision":M}`。

---

## 样例一：头一回登门（空记忆）

**目的**：证明「没有记忆时」应用照样能跑，而且掌柜不认识你。

**初始状态**：刚执行过 `/reset`，`/recall` 应返回 `count: 0`。

```bash
curl -s -X POST http://127.0.0.1:8765/recall \
  -H "Content-Type: application/json" -d '{"user_id":"阿七"}'
# 期望：{"user_id":"阿七","scope":"inn/阿七","count":0,"events":[],"has_memory":false}
```

**操作步骤**

1. 在 Dify 里新开一个会话。
2. 开始节点填「报上名号」= `阿七`。
3. 输入：`掌柜的，来碗阳春面。`

**预期表现**

- 语气客气，但会打听你来路（「面生得很」「打哪儿来」之类）。
- **不会**提到砸店、欠账、帮忙——因为账本是空的。
- 回复 30–90 字，没有旁白、没有「（微笑）」这类括号动作。

**实测**：见文末「实测记录」表第 1 行。

---

## 样例二：砸过店之后再进门（记忆改变态度）

**目的**：证明写进 PowerContext 的事件**真的改变了后续回复**——这是「技术结合有效」的核心证据。

**初始状态**：样例一结束后，账本里已有一条「点面」的记录。

**操作步骤**

1. 同一会话里继续输入：`我不吃了。上次我把你这儿的桌子都掀了，你忘了？`
2. 等她回完，再新开一个会话（**重要**：换会话才能证明是记忆在起作用，不是上下文）。
3. 名号仍然填 `阿七`，输入：`我又来了，还有面吗？`

**预期表现**

- 第 1 轮：她当场反应过来，把「掀桌子」这件事点出来，态度转冷/阴阳怪气。
- 第 2 轮（新会话）：**她仍然记得掀桌子这件事**，会主动提旧账、开价翻倍或者让你先赔钱。
- 两轮都不会说「我没有相关记录」——因为记忆确实被取到了。

**核对手法（这一步比看回复更硬）**

```bash
curl -s "http://127.0.0.1:8765/history?user_id=阿七"
```

应能看到按 revision 递增的条目，其中 `text` 字段就是玩家原话。同时
`dify/records/memory_api.log` 里每次 `recall` 都记录了 `returned` 与 `events_in_scope`——
第 2 轮新会话的 `events_in_scope` 必须 ≥ 1。

---

## 样例三：跨进程仍然记得（持久化）

**目的**：证明记忆不是存在内存里，重启后端也不丢。

**操作步骤**

1. 做完样例二，让账本里至少有 2 条事件。
2. **停掉后端的 uvicorn 进程**，再重新启动（同一条命令）。
3. 再查一次：

```bash
curl -s -X POST http://127.0.0.1:8765/recall \
  -H "Content-Type: application/json" -d '{"user_id":"阿七"}'
```

**预期表现**：返回的事件条数与重启前一致，内容相同。

**实测**：见文末「实测记录」。

---

## 如何恢复初始状态

`/reset` 是**追加一条 reset 标记**，不是删数据（符合 PowerContext 的 append-only 语义）：

- 重置之后 `/recall` 只返回标记之后的事件，等于「这一页账翻过去了」。
- 想彻底清空，删掉 `dify/data/memory.db` 再重启后端即可（库会自动重建）。
- `/history` 仍能看到 reset 之前的历史，便于核对「确实记过，只是翻篇了」。

---

## 实测记录

> 口径：下面每行的读数都来自本机实际执行，命令与输出一一对应。
> **Dify 端的结果列在实例导入验证完成后补填**；在你看到「已填」标记前，不要把本表当作 Dify 侧已验证。

### 后端（已实测）

复现环境：`_venv_dify`，`powercontext 1.2.0` / `fastapi 0.162.0` / `uvicorn 0.54.0` / `pydantic 2.14.1`，Python 3.13.12。

| # | 动作 | 读数 |
|---|---|---|
| 1 | `GET /health` | `{"status":"ok","db_url":"sqlite+aiosqlite:///.../dify/data/memory.db","db_exists":true,"probe_entries":0,"error":null}` |
| 2 | `POST /reset`（user_id=guest-001） | `{"ok":true,...,"entries_total":1,"revision":1}` |
| 3 | `POST /remember`（action=smash，text=他把两张桌子掀了…） | `{"ok":true,...,"entries_total":2,"revision":2}` |
| 4 | `POST /recall` | `count:1`，只返回 reset 之后那条砸店事件 —— 重置语义正确 |
| 5 | **重启 uvicorn 进程**后再 `POST /recall` | 仍返回同一条（`revision` 相同）—— 跨进程持久化成立 |
| 6 | `POST /remember_form`，`text=他说"这店也就那样"然后摔了个碗`（含英文双引号） | `{"ok":true,...,"revision":3}`；再 recall 该条文本**完整保存**，未被 JSON 转义截断 |
| 7 | `POST /recall_form`（Dify 实际调用的编码） | 与 JSON 版返回一致 |
| 8 | `python scripts/check_dsl.py`（结构层） | `节点 5 个，边 4 条`；`{answer:1, http-request:2, llm:1, start:1}`；无悬挂边、无非法变量引用 |
| 9 | `python scripts/check_dsl.py`（请求契约层） | `[想起旧账]` → `count:1, has_memory:true`（读到了上一轮写入的那条）；`[记下这笔账]` → `{"ok":true,...,"entries_total":2,"revision":2}` |

> 第 9 行是**真的把 DSL 里写死的 url 与 form 体按 Dify 的替换规则填好后发出去的**，
> 不是"看了一遍觉得没问题"。它当场抓到一个真缺陷：`/remember` 原来不回带 `user_id`，
> 与 `/recall` 不对称；已补齐（`user_id` / `scope` 一律回带），改后重跑通过。

### Dify 侧（已在本地 Dify 1.18.0 实例上实测）

环境：Dify **1.18.0**（docker compose，`--profile postgresql --profile weaviate`），Web 入口 `http://127.0.0.1:8080`。
应用 `记仇客栈`（`app_id = 893e3cfb-7b63-475e-9edc-21cbfe9a1b56`，mode `advanced-chat`）。

**导入结果**：`POST /console/api/apps/imports` → `status = completed-with-warnings`；
`imported_dsl_version = 0.1.5` 被自动升到 `current_dsl_version = 0.7.0`；
导入后读草稿：**节点 5 个 / 边 4 条，5 个节点 id 与类型全部与 DSL 一致**（0 缺失、0 类型不符）。

**运行结果**（`POST /console/api/apps/<id>/advanced-chat/workflows/draft/run`，SSE 全量消费）：

| # | 样例 | 节点结果 | 关键读数 |
|---|---|---|---|
| 1 | 头一回登门（`guest_name=阿七`，账本空） | 开始 ✓ → 想起旧账 ✓ → 掌柜的嘴 ✓ → 记下这笔账 ✓ → 掌柜开口 ✓ | recall `count=0, has_memory=false`；remember `entries_total=1, revision=1` |
| 2 | 再进一次门（同一名号，账本已有 1 条） | 同上 5 个节点全 ✓ | recall `count=1`（读到了上一轮那条）；remember `revision=2` |
| 3 | 连跑（账本 2 条后） | 同上 5 个节点全 ✓ | recall `count=2`；LLM 节点输出 `又是你。上回掀桌子那笔账我可记着，房钱翻倍，先赔碗再谈住不住。`；`掌柜开口` 输出同样的文本；remember `revision=3` |

⇒ **「写进 PowerContext 的记忆，在下一轮被 Dify 读出来并改变了回复」这条链路，是实际跑通的**，
不是设计稿：`records/memory_api.log` 里能逐条对上 `recall` / `remember` 的时间与 revision。

**⚠️ 必须说明的两点（否则不算如实）**：

1. **LLM 节点连的是本地假模型**（`scripts/mock_llm.py`，OpenAI 兼容、支持流式），
   不是真的 MiniMax：本机没有可用的大模型 Key。它按账本内容返回固定话术，
   **只证明「节点链路通、记忆进了 prompt」**，不证明模型效果。
   换成真 Key 只需在 Dify「设置 → 模型供应商」里把 `OpenAI-API-compatible` 的
   `endpoint_url` / `api_key` 换成 `https://api.minimaxi.com/v1` 与真 Key，DSL 不用改。
2. **要让 HTTP 节点能访问宿主机后端，必须放开 SSRF 白名单**。Dify 默认会拦截私有网段：
   ```
   # docker/.env
   SSRF_PROXY_ALLOW_PRIVATE_IPS=172.16.0.0/12,192.168.0.0/16,10.0.0.0/8
   ```
   改完要 `docker compose up -d ssrf_proxy`（白名单由 ssrf_proxy 容器的 entrypoint 写进 squid）。
   不放开的话报错原文：`Access to 'http://host.docker.internal:8765/recall_form' was blocked by SSRF protection`。

**复现脚本**：`scripts/verify_dify.py`（登录 → 导入 → 发布 → 跑一行并打印每个节点的状态与输出）。

**运行证据（原始输出，随包留档）**：

- `records/memory_api.log` —— 后端每一次 `recall` / `remember` 的原始记录（时间、名号、scope、count、revision），对得上上面那张表的每一行。
- `records/verify_run.txt` —— `scripts/verify_dify.py` **第 4 次**完整运行的原始 stdout（导入 → 发布 → 5 个节点逐条状态），未做任何编辑。

第 4 次运行还顺带证明了一件比截图更硬的事：这次导入出来的是**全新的** `app_id = 94e8f951-323c-434c-a706-b75d4c6be161`，
而「想起旧账」节点一上来就读到 `count=4`，写入后 `entries_total=5`。
⇒ 记忆不在 Dify 的应用状态里，而在后端库（`data/memory.db`）里 —— 换句话说，**换一个 app 也认得这个人**。

**若要界面截图（题面 4.3 若明确要求"截图"）—— 三步，约 30 秒**：

1. 浏览器打开 `http://127.0.0.1:8080`，用 `verify@local.test` / `VerifyPass123!` 登录
   （这是本机验证时建的本地账号，不是任何真实账号）。
2. 左侧「记仇客栈」→ 画布页截一张（能看到 5 个节点和连线）。
3. 右上「预览」→ 输入「我又来了，还有面吗？」→ 发送，等回复出来再截一张
   （能看到「想起旧账」读到 count、以及掌柜那句台词）。

⚠️ 交件前检查截图里有没有带上顶部邮箱 / 工作区名，有就裁掉。

**踩过的坑（留给下一个跑的人）**：

| 现象 | 真因 | 处置 |
|---|---|---|
| `docker pull langgenius/dify-api` 一直 `EOF` | Docker Hub 的 blob 走代理拿不到；国内镜像源的大层在 Docker 侧反复断 | 用 `crane pull` 逐层下载后拼成 OCI 归档 `docker load`（本机实测：Docker 直拉 0 层 / crane 可下但会中断 / 自写续传脚本 15 分钟拉完 916 MB） |
| `login` 报 `Invalid encrypted data` | 1.18 的 `password` 字段要求 **base64** | 传 `base64(password)` |
| `POST /apps/import` 404 | 1.18 改成 `/apps/imports`（两段式），且要 **CSRF 头** `X-CSRF-Token`（取自登录后的 cookie） | 见 `verify_dify.py` |
| 保存草稿 409 `draft_workflow_not_sync` | 必须回传草稿的 `hash` | 先 GET 草稿再整体提交 |
| 模型供应商列表为空 | 1.x 的模型供应商是插件，镜像里不自带 | 从 marketplace 下 `openai_api_compatible` 的 `.difypkg`，`plugin/upload/pkg` + `plugin/install/pkg` 两步装 |
