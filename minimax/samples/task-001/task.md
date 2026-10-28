# 任务 T-001：为 taskboard 实现标签筛选

## 背景

TaskBoard 的 `filter_tasks` 已经支持按状态（`status`）和最低优先级（`min_priority`）筛选。
接口上早就有 `tag` 参数，但实现里一直是空转——传了也不生效，函数体里留着一行
`TODO(SPEC-001#R2)`。

产品侧现在要求真正支持按标签筛选，规格文档已从 v1 更新到 v2。

## 要改什么

`demo_project/taskboard/service.py` 中的 `filter_tasks` 函数，实现 `tag` 参数：

- 只保留 `tags` 中包含该标签的任务；
- 匹配**不区分大小写**，比较前去掉 `tag` 两侧空白；
- `tag` 为 `None` 或去空白后为空字符串时，视为不筛选；
- 与 `status`、`min_priority` 同时给出时取交集；
- 存在 `tag` 筛选但没有任何任务命中时，返回空列表。

## 哪些原有行为必须保持

- `status` 的三种取值与非法值抛 `ValueError` 的行为不变；
- `min_priority` 的「含下界」语义不变；
- 返回结果的顺序与入参顺序一致；
- `summarize` 不受影响。

## 验收

同一组测试在修改前后都会执行：

- `tests/test_filter_by_tag.py`（目标功能，修改前 5 项失败，修改后必须全过）
- `tests/test_service.py`（回归，修改前后均须全过）

## 规格依据

- `samples/task-001/spec-v2/SPEC-001-task-filter.md`（v2，当前生效）
- v1 见 `specs/SPEC-001-task-filter.md`（已被 v2 取代，保留用于演示规格更新）
