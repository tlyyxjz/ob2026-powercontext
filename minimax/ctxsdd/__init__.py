"""ctxsdd —— 面向规格驱动开发（SDD）的工程上下文模块。

模块职责：
- 把规格条目、代码符号、测试用例导入 seekdb 并建立可检索索引（``store`` / ``importer``）；
- 按开发任务检索并组装「该遵守哪条规格、该改哪个符号、该跑哪些测试」（``selector``）；
- 通过 PowerContext 持久化任务进展与验证结果，供后续运行复用（``state``）；
- 调用 MiniMax 依据上下文生成代码修改（``minimax_client`` / ``agent``）。

调用入口见 ``ctxsdd.cli``，或 ``python -m ctxsdd --help``。
"""

from ctxsdd.config import Settings

__all__ = ["Settings"]
