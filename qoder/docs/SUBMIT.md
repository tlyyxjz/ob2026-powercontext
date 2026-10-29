# Qoder 赛道 · 提交材料清单与填写卡片

> 依据：题面原文（`24.md`，语雀存档 2026-10-28 抓取）§4「提交材料」与 §5「提交方式」。
> 本文件只列**要填什么、放在哪、怎么自查**，不写实现细节。

---

## 一、平台要填的字段

| 字段 | 填什么 | 现在有没有 |
|---|---|---|
| 项目网站网址 | `https://review-interviewer-qpydgq8t0ig.qoder.zone`（`access_mode=public`，已实测 HTTP 200） | ✅ |
| 代码仓库 | `https://github.com/tlyyxjz/ob2026-powercontext` | ✅ |
| Qoder 账号 UID | `019fd088-8e20-78d5-9ec5-587d476c50b2` | ✅ |
| 分支 / commit | `2026-competition-ecosystem` / `68988b387c1c6a6ffa59a988e0189c9cfdd85fae`（回读 `git ls-remote` 确认） | ✅ |
| 材料目录 | 分支上的 `qoder/` | ✅ 25 个文件、6525 行 |

⚠️ 题面第一段原文：「在完成私有 repo 导入后，你**已经获得一个同名的分支**」⇒ 直接用 `2026-competition-ecosystem`，
不要另起 `qoder-impl` 之类的新分支名。

---

## 二、题面 §4 的五项材料，逐项对照

| # | 材料 | 题面要求（逐字要点） | 落在哪个文件 | 状态 |
|---|---|---|---|---|
| 1 | 项目网站网址 | 可见范围「任何人都可访问」 | 平台字段 | ✅ 已公开，HTTP 200 |
| 2 | 代码仓库 | 完整源码，公开或向评审开放 | 平台字段 | ✅ |
| 3 | 运行说明 | 运行环境、依赖版本、seekdb/PowerContext 部署与接入、从安装到启动的完整步骤 | `qoder/README.md` | ✅ |
| 4 | 验证样例 | 「至少一组」，含初始数据、输入、操作步骤、预期结果，以及指定技术发挥作用的记录 | `qoder/docs/VERIFY.md` | ✅ 5 组样例，全部带真实读数 |
| 5 | Qoder 开发记录 | 关键环节的对话截图或录屏，**并附 Qoder 账号 UID** | `qoder/docs/development-log.md` + 截图 | ✅ 流水 6 轮已导出；截图见下节 |

## 1.5 发布过程中踩到的一个坑（备查）

`prepare_site` 第一次调用会把入参指纹冻进本地快照（`~/.qoder-cn/sites/state/publishing/`）。
之后再改任何一个字段都会报 `sites_conflict`（防篡改，**不是撞名**），
而不改又会因为 `createNewSite:true` 时缺 `slug` 被服务端拒（`sites_invalid_request`）。
两者构成死结 ⇒ **只能换一个全新的 actionId，从第一次调用就带全 `slug`**。

正确入参（本项目的实际值）：

```json
{"actionId":"<新 UUIDv4>","projectRoot":"<qoder 目录绝对路径>","webDirectory":"web/site",
 "createNewSite":true,"displayName":"复盘面试官","slug":"review-interviewer"}
```

`slug` 正则是 `^[a-z0-9][a-z0-9-]{0,62}$`（纯 ASCII），中文 `displayName` 派生不出地址，
所以必须显式给。

鼓励项：使用效果（人数、次数、提效对比、反馈，附来源）。站点用 `get_analytics` 取 PV/UV，
但**UV 必须用服务端总数，不能自己加总每日序列**（该操作说明原文如此）。

---

## 三、四条红线自查（提交前逐条过）

| # | 红线 | 自查方式 |
|---|---|---|
| 1 | 不许「别处开发、Qoder 只发站」 | 开发记录里能看出每一段需求 → 改了哪些文件 → 报错怎么修的 |
| 2 | 不许「装了就算用过」 | 验证样例里要有**记忆读写前后的实际记录**（revision 从几变到几、entries 原文） |
| 3 | 公开访问必须个人账号 | `qodercn status` 的 `User Type` 不能是 `teams` |
| 4 | 评测结束前别删站点 | 发布后不要点删除/暂停；改内容要重新 `publish_site` |

另外两条题面原文约束：
- 「配置示例使用占位值，**不提交真实 API Key、密码或其他凭据**」
- 「复用开源代码或数据时，**注明来源及所作改动**」——PowerContext 属复用，README 里要写清版本与改了什么

---

## 四、提交前 60 秒自查

1. 无痕窗口打开站点 → 能开
2. 站点里没有 `sk-`、`token`、内网 IP
3. `git ls-remote` 回读分支 tip，与平台填的 commit 一致
4. 分支上 `qoder/` 目录含 README / docs/VERIFY.md / docs/development-log.md / 源码
5. 平台填的 UID 与发布站点账号一致
