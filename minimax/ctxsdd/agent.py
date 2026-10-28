"""把上下文交给 MiniMax，拿到代码修改并落地。

设计取舍：让模型输出**完整的单个函数源码**，而不是 unified diff。
原因：diff 的行号与上下文极易错位，一旦错位就无法自动应用；而整函数替换
可以用 ``ast`` 精确定位到原函数的行区间，替换后立刻做语法校验，
失败时保持原文件不变。
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctxsdd.config import Settings
from ctxsdd.minimax_client import MiniMaxClient, MiniMaxReply

CODE_BLOCK = re.compile(r"```(?:python)?\s*\n(?P<code>.*?)```", re.DOTALL)

SYSTEM_PROMPT = (
    "你是一名严谨的 Python 后端工程师。你只依据给定的规格与代码工作，"
    "不臆造未声明的行为，不引入第三方依赖。"
)


@dataclass(frozen=True)
class ProposeResult:
    reply: MiniMaxReply
    code: str
    patch_path: Path
    diff: str
    target: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_code(text: str) -> str:
    """从模型输出中取出第一段 Python 代码块；没有代码块时按整体处理。"""

    match = CODE_BLOCK.search(text)
    code = match.group("code") if match else text
    return code.strip("\n")


def _function_span(source: str, name: str) -> tuple[int, int, str]:
    """返回模块级函数 ``name`` 的 (起始行, 结束行, 源码片段)，行号 1 起。"""

    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            end = getattr(node, "end_lineno", node.lineno)
            lines = source.splitlines(keepends=True)
            return node.lineno, end, "".join(lines[node.lineno - 1 : end])
    raise LookupError(f"模块级函数 {name!r} 不存在")


def build_prompt(
    *,
    task: str,
    spec_items: list[dict[str, Any]],
    source_path: Path,
    function_name: str,
    test_names: list[str],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """组装发给 MiniMax 的请求，同时返回可写入开发记录的上下文摘要。"""

    source = _read(source_path)
    start, end, snippet = _function_span(source, function_name)

    spec_block = "\n\n".join(
        f"[{row['id']} · {row['title']} · 来源 {row['source_path']}]\n{row['body']}"
        for row in spec_items
    )
    prompt = f"""# 开发任务
{task}

# 必须遵守的规格条目
{spec_block}

# 当前实现（待修改，来自 {source_path.as_posix()} 第 {start}-{end} 行）
```python
{snippet.rstrip()}
```

# 验收测试（修改后必须全部通过，当前为失败状态）
需要满足的测试：{", ".join(test_names)}

# 输出要求
1. 只输出修改后的 **完整 `{function_name}` 函数源码**，包含 `def` 行与全部函数体。
2. 必须放在一个 ```python 代码块内，代码块之外不要写任何解释文字。
3. 保持函数签名、参数名、关键字参数形式、docstring 风格与现有错误处理不变。
4. 只修改 `{function_name}`，不得输出其他函数或新增模块级依赖。
5. 只能使用 Python 标准库。

开始输出。"""

    summary = {
        "规格条目": ", ".join(row["id"] for row in spec_items),
        "代码位置": f"{source_path.as_posix()}:{start}-{end}",
        "目标函数": function_name,
        "覆盖测试": ", ".join(test_names),
        "提示词长度": f"{len(prompt)} 字符",
    }
    return (
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        summary,
    )


def propose(
    settings: Settings,
    *,
    task_id: str,
    task: str,
    spec_items: list[dict[str, Any]],
    source_path: Path,
    function_name: str,
    test_names: list[str],
) -> ProposeResult:
    """调用 MiniMax 生成修改，保存原始输出与补丁，不写回源码。"""

    client = MiniMaxClient(settings)
    messages, summary = build_prompt(
        task=task,
        spec_items=spec_items,
        source_path=source_path,
        function_name=function_name,
        test_names=test_names,
    )
    reply = client.chat(messages)
    code = extract_code(reply.content)

    # 语法校验：模型给的东西必须本身是可解析的 Python
    try:
        ast.parse(code)
    except SyntaxError as exc:
        raise SystemExit(f"模型返回的代码无法解析为 Python：{exc}\n---\n{code[:800]}") from exc

    records = settings.records_dir
    records.mkdir(parents=True, exist_ok=True)

    raw_path = records / f"{task_id}.minimax_raw.txt"
    raw_path.write_text(reply.content, encoding="utf-8")

    patch_path = records / f"{task_id}.minimax_patch.py"
    patch_path.write_text(code + "\n", encoding="utf-8")

    original = _function_span(_read(source_path), function_name)[2]
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            (code + "\n").splitlines(keepends=True),
            fromfile=f"a/{source_path.name}",
            tofile=f"b/{source_path.name}",
        )
    )
    (records / f"{task_id}.minimax.diff").write_text(diff, encoding="utf-8")

    client.log_exchange(
        task_id=task_id,
        purpose="生成代码修改",
        prompt_summary=summary,
        reply=reply,
        output_rel_path=f"records/{task_id}.minimax_patch.py",
    )

    return ProposeResult(
        reply=reply,
        code=code,
        patch_path=patch_path,
        diff=diff,
        target=function_name,
    )


def apply_patch(source_path: Path, *, function_name: str, new_code: str) -> str:
    """用新函数源码替换原函数，返回 unified diff。

    替换前校验新代码语法，替换后仍校验整文件语法；任何一步失败都不落盘。
    """

    source = _read(source_path)
    start, end, _old = _function_span(source, function_name)

    replacement = new_code.rstrip("\n") + "\n"
    lines = source.splitlines(keepends=True)
    updated = "".join(lines[: start - 1]) + replacement + "".join(lines[end:])

    try:
        ast.parse(updated)
    except SyntaxError as exc:
        raise SystemExit(f"替换后整文件语法错误，已放弃写入：{exc}") from exc

    diff = "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{source_path.name}",
            tofile=f"b/{source_path.name}",
        )
    )
    source_path.write_text(updated, encoding="utf-8")
    return diff
