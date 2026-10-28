# 记仇客栈 · 配套后端（PowerContext 记忆服务）

把 PowerContext 的 Memory artifact 包成 HTTP 端点，给 Dify 的 HTTP Request 节点调用。
**不含业务逻辑**——谁说什么话全在 Dify 的 LLM 节点里，这里只负责「记住」和「想起」。

## 启动

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn memory_api:app --host 0.0.0.0 --port 8765
```

必须有 `--host 0.0.0.0`（Dify 在容器里，`127.0.0.1` 连不到宿主机）。

## 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 健康检查；`status: ok` 才可用 |
| POST | `/recall` | JSON：`{user_id, limit}` → 返回该名号的事件列表 |
| POST | `/remember` | JSON：`{user_id, action, text, reply}` → 追加一条事件 |
| POST | `/reset` | JSON：`{user_id}` → 追加重置标记，此后 `/recall` 只返回标记之后的事件 |
| GET | `/history` | `?user_id=` 原始条目，人工核对记忆内容用 |
| POST | `/recall_form` | 同上，但收 `x-www-form-urlencoded`（**Dify 实际调的是这个**） |
| POST | `/remember_form` | 同上，收表单编码 |

## 为什么有两套编码

Dify 的 HTTP Request 节点做变量替换时**不做 JSON 转义**。玩家一句话里带个英文双引号，
`{"text": "他说"这店也就那样""}` 这种 body 就直接废了。改用 `x-www-form-urlencoded`
由 Dify 自己编码、FastAPI 自己解码，绕开这个问题（实测：带引号的文本完整存下来了）。

## 数据与日志

- 库：`../data/memory.db`（PowerContext 的 SQLite，自动创建；**不要提交**）
- 日志：`../records/memory_api.log`，每次读写一行 JSON，含 `revision` 与条数——验收时对照这份日志核对「记忆真的写进去了、真的读回来了」

想换库位置，用环境变量：

```bash
INN_DB_PATH=/path/to/memory.db python -m uvicorn memory_api:app --host 0.0.0.0 --port 8765
```

## 依赖

`fastapi` / `uvicorn[standard]` / `powercontext[builtin]==1.2.0`（版本锁定，与比赛冻结版本同代）。
