"""把规格、代码符号与测试用例解析成可检索的记录。

解析全部基于标准库（正则 + ``ast``），不引入额外解析依赖，保证评审环境
只要装了 Python 就能复现导入过程。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ctxsdd.config import ROOT

SPEC_HEADING = re.compile(r"^##\s+(?P<item>[A-Z]+-?\d+)\s+(?P<title>.+?)\s*$", re.MULTILINE)
VERSION_LINE = re.compile(r"^-\s*版本[:：]\s*(?P<version>\S+)\s*$", re.MULTILINE)
DOC_ID = re.compile(r"^(?P<doc>[A-Z]+-\d+)")


def relative_to_root(path: Path) -> str:
    """尽量用相对本模块根的路径记录来源，保证材料可移植。"""

    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class SpecDoc:
    path: Path
    doc_id: str
    version: str
    items: tuple[dict[str, str], ...]


def parse_spec_file(path: Path) -> SpecDoc:
    """解析一份规格 Markdown，按 ``## <条目号> <标题>`` 切分。

    文件头部的 ``- 版本：vX`` 决定条目所属版本；同一份文件内所有条目共享该版本。
    """

    text = path.read_text(encoding="utf-8")
    stem = path.stem
    match = DOC_ID.match(stem)
    doc_id = match.group("doc") if match else stem

    version_match = VERSION_LINE.search(text)
    version = version_match.group("version") if version_match else "unversioned"

    headings = list(SPEC_HEADING.finditer(text))
    items: list[dict[str, str]] = []
    for index, heading in enumerate(headings):
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[start:end].strip()
        items.append(
            {
                "item_id": heading.group("item"),
                "title": heading.group("title"),
                "body": body,
            }
        )
    return SpecDoc(path=path, doc_id=doc_id, version=version, items=tuple(items))


def _iter_py_files(root: Path, *, exclude_dirs: set[str] | None = None) -> Iterator[Path]:
    skip = {"__pycache__", ".venv", "build"} | (exclude_dirs or set())
    for path in sorted(root.rglob("*.py")):
        if any(part in skip for part in path.parts):
            continue
        yield path


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str]) -> str:
    """从源码中还原函数签名（取 def 行到参数结束）。"""

    line = source_lines[node.lineno - 1].strip()
    # 单行签名直接返回；多行签名取到出现 ':' 的物理行为止
    if line.rstrip().endswith(":"):
        return line.rstrip(":")
    parts = [line]
    for offset in range(node.lineno, min(node.lineno + 12, len(source_lines))):
        candidate = source_lines[offset].strip()
        parts.append(candidate)
        if candidate.rstrip().endswith(":"):
            break
    joined = " ".join(parts)
    return joined.rstrip(":")


def parse_code_symbols(root: Path, *, project_root: Path | None = None) -> list[dict[str, Any]]:
    """用 ``ast`` 提取模块级类与函数，含行号区间与文档字符串。

    测试目录单独由 :func:`parse_test_cases` 处理，这里排除，
    避免把 ``test_*`` 函数同时算作业务代码。
    """

    rows: list[dict[str, Any]] = []
    base = project_root or root
    for path in _iter_py_files(root, exclude_dirs={"tests", "test"}):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            continue
        source_lines = source.splitlines()
        rel = path.relative_to(base).as_posix() if base in path.parents else path.as_posix()

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "function"
                doc = ast.get_docstring(node) or ""
                signature = _signature(node, source_lines)
                end_line = getattr(node, "end_lineno", node.lineno)
                rows.append(
                    {
                        "id": f"{rel}::{node.name}",
                        "path": rel,
                        "symbol": node.name,
                        "kind": kind,
                        "start_line": node.lineno,
                        "end_line": end_line,
                        "signature": signature,
                        "doc": doc,
                        "imported_at": _now(),
                    }
                )
                for inner in node.body:
                    if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        rows.append(
                            {
                                "id": f"{rel}::{node.name}.{inner.name}",
                                "path": rel,
                                "symbol": f"{node.name}.{inner.name}",
                                "kind": "method",
                                "start_line": inner.lineno,
                                "end_line": getattr(inner, "end_lineno", inner.lineno),
                                "signature": _signature(inner, source_lines),
                                "doc": ast.get_docstring(inner) or "",
                                "imported_at": _now(),
                            }
                        )
            elif isinstance(node, ast.ClassDef):
                rows.append(
                    {
                        "id": f"{rel}::{node.name}",
                        "path": rel,
                        "symbol": node.name,
                        "kind": "class",
                        "start_line": node.lineno,
                        "end_line": getattr(node, "end_lineno", node.lineno),
                        "signature": f"class {node.name}",
                        "doc": ast.get_docstring(node) or "",
                        "imported_at": _now(),
                    }
                )
                for inner in node.body:
                    if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        rows.append(
                            {
                                "id": f"{rel}::{node.name}.{inner.name}",
                                "path": rel,
                                "symbol": f"{node.name}.{inner.name}",
                                "kind": "method",
                                "start_line": inner.lineno,
                                "end_line": getattr(inner, "end_lineno", inner.lineno),
                                "signature": _signature(inner, source_lines),
                                "doc": ast.get_docstring(inner) or "",
                                "imported_at": _now(),
                            }
                        )
    return rows


COVERS = re.compile(r"SPEC-\d+#[A-Z]+-?\d+")


def parse_test_cases(tests_root: Path, *, project_root: Path) -> list[dict[str, Any]]:
    """提取 ``test_*`` 函数，并从 docstring 中识别它覆盖的规格条目。"""

    rows: list[dict[str, Any]] = []
    for path in _iter_py_files(tests_root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = path.relative_to(project_root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                doc = ast.get_docstring(node) or ""
                covers = sorted(set(COVERS.findall(f"{node.name} {doc}")))
                rows.append(
                    {
                        "id": f"{rel}::{node.name}",
                        "path": rel,
                        "name": node.name,
                        "covers": ",".join(covers) if covers else None,
                        "doc": doc,
                        "imported_at": _now(),
                    }
                )
    return rows


def build_rows(
    *, specs_dir: Path, project_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """解析一个「规格目录 + 业务工程」，返回三张表的行与规格版本集合。"""

    spec_rows: list[dict[str, Any]] = []
    versions: list[str] = []
    for spec_path in sorted(specs_dir.rglob("*.md")):
        doc = parse_spec_file(spec_path)
        if not doc.items:
            continue
        versions.append(doc.version)
        for item in doc.items:
            spec_rows.append(
                {
                    "id": f"{doc.doc_id}#{item['item_id']}@{doc.version}",
                    "doc_id": doc.doc_id,
                    "spec_version": doc.version,
                    "item_id": item["item_id"],
                    "title": item["title"],
                    "body": item["body"],
                    "source_path": relative_to_root(spec_path),
                    "imported_at": _now(),
                }
            )

    package_root = project_dir
    symbol_rows = parse_code_symbols(package_root, project_root=project_dir)
    tests_root = project_dir / "tests"
    case_rows = parse_test_cases(tests_root, project_root=project_dir) if tests_root.is_dir() else []
    return spec_rows, symbol_rows, case_rows, versions
