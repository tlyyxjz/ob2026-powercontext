"""seekdb 存储层：工程资料的落库、检索与查询留痕。

数据落在 seekdb（MySQL 协议）；每次检索都会写入 ``query_log``，
用于生成「数据库查询记录」证据。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import pymysql

from ctxsdd.config import Settings

DDL = [
    """
    CREATE TABLE IF NOT EXISTS spec_items (
        id VARCHAR(160) PRIMARY KEY,
        doc_id VARCHAR(64) NOT NULL,
        spec_version VARCHAR(24) NOT NULL,
        item_id VARCHAR(24) NOT NULL,
        title VARCHAR(255) NOT NULL,
        body TEXT NOT NULL,
        source_path VARCHAR(400) NOT NULL,
        imported_at DATETIME NOT NULL,
        KEY idx_spec_doc (doc_id, spec_version)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS code_symbols (
        id VARCHAR(300) PRIMARY KEY,
        path VARCHAR(400) NOT NULL,
        symbol VARCHAR(255) NOT NULL,
        kind VARCHAR(32) NOT NULL,
        start_line INT NOT NULL,
        end_line INT NOT NULL,
        signature TEXT,
        doc TEXT,
        imported_at DATETIME NOT NULL,
        KEY idx_sym_name (symbol)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS test_cases (
        id VARCHAR(300) PRIMARY KEY,
        path VARCHAR(400) NOT NULL,
        name VARCHAR(255) NOT NULL,
        covers VARCHAR(600),
        doc TEXT,
        imported_at DATETIME NOT NULL,
        KEY idx_case_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS import_runs (
        run_id VARCHAR(64) PRIMARY KEY,
        started_at DATETIME NOT NULL,
        spec_versions VARCHAR(255) NOT NULL,
        spec_count INT NOT NULL,
        symbol_count INT NOT NULL,
        case_count INT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS query_log (
        seq BIGINT AUTO_INCREMENT PRIMARY KEY,
        run_id VARCHAR(64) NOT NULL,
        purpose VARCHAR(64) NOT NULL,
        statement TEXT NOT NULL,
        params TEXT NOT NULL,
        row_count INT NOT NULL,
        logged_at DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

_STOPWORDS = {
    "的", "了", "为", "在", "与", "和", "及", "或", "按", "对", "把", "被",
    "增加", "实现", "支持", "需要", "一个", "进行", "应该", "如何", "什么",
    "the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "with",
    "add", "support", "should", "need", "task", "please",
}

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def extract_keywords(text: str) -> list[str]:
    """从任务描述里抽取检索关键词。

    中文按二元组切分（无分词器时的稳定做法），英文按下划线/驼峰标识符原样保留。
    """

    words: list[str] = []
    for token in _LATIN.findall(text):
        lowered = token.lower()
        if lowered not in _STOPWORDS and len(lowered) > 1:
            words.append(lowered)

    for chunk in _CJK.findall(text):
        if len(chunk) == 1:
            continue
        for i in range(len(chunk) - 1):
            bigram = chunk[i : i + 2]
            if bigram not in _STOPWORDS:
                words.append(bigram)

    seen: set[str] = set()
    ordered: list[str] = []
    for word in words:
        if word not in seen:
            seen.add(word)
            ordered.append(word)
    return ordered


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Store:
    """seekdb 上的工程资料库。"""

    settings: Settings

    def connect(self, *, with_database: bool = True) -> pymysql.connections.Connection:
        kwargs: dict[str, Any] = dict(
            host=self.settings.seekdb_host,
            port=self.settings.seekdb_port,
            user=self.settings.seekdb_user,
            password=self.settings.seekdb_password,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
        )
        if with_database:
            kwargs["database"] = self.settings.seekdb_database
        return pymysql.connect(**kwargs)

    # ---------- 初始化 ----------

    def init_schema(self) -> dict[str, Any]:
        """重建数据库与全部表。返回 seekdb 版本，便于留证。

        ``init`` 的语义是「回到初始状态」，因此会先删表再建表：
        资料表可由 ``import`` 完整重建，重复执行得到的结果完全一致。
        """

        conn = self.connect(with_database=False)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                version = cur.fetchone()[0]
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{self.settings.seekdb_database}` "
                    "DEFAULT CHARSET utf8mb4"
                )
                cur.execute(f"USE `{self.settings.seekdb_database}`")
                for table in ("query_log", "import_runs", "test_cases", "code_symbols", "spec_items"):
                    cur.execute(f"DROP TABLE IF EXISTS {table}")
                for statement in DDL:
                    cur.execute(statement)
        finally:
            conn.close()
        return {"seekdb_version": version, "database": self.settings.seekdb_database}

    # ---------- 写入 ----------

    def replace_corpus(
        self,
        *,
        run_id: str,
        spec_rows: Sequence[dict[str, Any]],
        symbol_rows: Sequence[dict[str, Any]],
        case_rows: Sequence[dict[str, Any]],
        spec_versions: Iterable[str],
    ) -> dict[str, int]:
        """以一次导入为界整体替换三张资料表，并登记导入批次。

        整体替换保证「重新导入后不再残留旧版本条目」，这是规格更新能被
        正确使用的前提。
        """

        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM spec_items")
                cur.execute("DELETE FROM code_symbols")
                cur.execute("DELETE FROM test_cases")

                if spec_rows:
                    cur.executemany(
                        "INSERT INTO spec_items "
                        "(id, doc_id, spec_version, item_id, title, body, source_path, imported_at) "
                        "VALUES (%(id)s, %(doc_id)s, %(spec_version)s, %(item_id)s, %(title)s, "
                        "%(body)s, %(source_path)s, %(imported_at)s)",
                        list(spec_rows),
                    )
                if symbol_rows:
                    cur.executemany(
                        "INSERT INTO code_symbols "
                        "(id, path, symbol, kind, start_line, end_line, signature, doc, imported_at) "
                        "VALUES (%(id)s, %(path)s, %(symbol)s, %(kind)s, %(start_line)s, "
                        "%(end_line)s, %(signature)s, %(doc)s, %(imported_at)s)",
                        list(symbol_rows),
                    )
                if case_rows:
                    cur.executemany(
                        "INSERT INTO test_cases (id, path, name, covers, doc, imported_at) "
                        "VALUES (%(id)s, %(path)s, %(name)s, %(covers)s, %(doc)s, %(imported_at)s)",
                        list(case_rows),
                    )

                cur.execute(
                    "INSERT INTO import_runs "
                    "(run_id, started_at, spec_versions, spec_count, symbol_count, case_count) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE spec_versions=VALUES(spec_versions), "
                    "spec_count=VALUES(spec_count), symbol_count=VALUES(symbol_count), "
                    "case_count=VALUES(case_count)",
                    (
                        run_id,
                        _now(),
                        ",".join(sorted(set(spec_versions))),
                        len(spec_rows),
                        len(symbol_rows),
                        len(case_rows),
                    ),
                )
        finally:
            conn.close()

        return {
            "spec_items": len(spec_rows),
            "code_symbols": len(symbol_rows),
            "test_cases": len(case_rows),
        }

    def log_query(
        self, *, run_id: str, purpose: str, statement: str, params: Any, row_count: int
    ) -> None:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO query_log (run_id, purpose, statement, params, row_count, logged_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (run_id, purpose, statement, json.dumps(params, ensure_ascii=False), row_count, _now()),
                )
        finally:
            conn.close()

    # ---------- 检索 ----------

    def _search_specs(self, keywords: Sequence[str], limit: int) -> list[dict[str, Any]]:
        if not keywords:
            return []
        clauses = []
        params: list[Any] = []
        for kw in keywords:
            clauses.append("(title LIKE %s OR body LIKE %s)")
            params.extend([f"%{kw}%", f"%{kw}%"])
        sql = (
            "SELECT id, doc_id, spec_version, item_id, title, body, source_path FROM spec_items "
            f"WHERE {' OR '.join(clauses)}"
        )
        conn = self.connect()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, params)
                rows = list(cur.fetchall())
        finally:
            conn.close()

        def score(row: dict[str, Any]) -> tuple[int, int]:
            title_hits = sum(1 for kw in keywords if kw in row["title"].lower())
            body_hits = sum(1 for kw in keywords if kw in row["body"].lower())
            return (title_hits, body_hits)

        rows.sort(key=score, reverse=True)
        self.log_query(
            run_id="search-specs",
            purpose="select_specs",
            statement=sql,
            params=keywords,
            row_count=len(rows),
        )
        return rows[:limit]

    def _search_symbols(self, keywords: Sequence[str], limit: int) -> list[dict[str, Any]]:
        if not keywords:
            return []
        clauses = []
        params: list[Any] = []
        for kw in keywords:
            clauses.append("(LOWER(symbol) LIKE %s OR LOWER(doc) LIKE %s OR LOWER(path) LIKE %s)")
            params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])
        sql = (
            "SELECT id, path, symbol, kind, start_line, end_line, signature, doc FROM code_symbols "
            f"WHERE {' OR '.join(clauses)}"
        )
        conn = self.connect()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, params)
                rows = list(cur.fetchall())
        finally:
            conn.close()

        def score(row: dict[str, Any]) -> tuple[int, int, int]:
            name_hits = sum(1 for kw in keywords if kw in row["symbol"].lower())
            doc_hits = sum(1 for kw in keywords if kw in (row["doc"] or "").lower())
            path_hits = sum(1 for kw in keywords if kw in row["path"].lower())
            return (name_hits, doc_hits, path_hits)

        rows.sort(key=score, reverse=True)
        self.log_query(
            run_id="search-symbols",
            purpose="select_symbols",
            statement=sql,
            params=keywords,
            row_count=len(rows),
        )
        return rows[:limit]

    def _search_tests(self, keywords: Sequence[str], limit: int) -> list[dict[str, Any]]:
        if not keywords:
            return []
        clauses = []
        params: list[Any] = []
        for kw in keywords:
            clauses.append("(LOWER(name) LIKE %s OR LOWER(doc) LIKE %s OR LOWER(COALESCE(covers,'')) LIKE %s)")
            params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])
        sql = (
            "SELECT id, path, name, covers, doc FROM test_cases "
            f"WHERE {' OR '.join(clauses)}"
        )
        conn = self.connect()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, params)
                rows = list(cur.fetchall())
        finally:
            conn.close()

        def score(row: dict[str, Any]) -> tuple[int, int, int]:
            name_hits = sum(1 for kw in keywords if kw in row["name"].lower())
            doc_hits = sum(1 for kw in keywords if kw in (row["doc"] or "").lower())
            cover_hits = sum(1 for kw in keywords if kw in (row["covers"] or "").lower())
            return (name_hits, cover_hits, doc_hits)

        rows.sort(key=score, reverse=True)
        self.log_query(
            run_id="search-tests",
            purpose="select_tests",
            statement=sql,
            params=keywords,
            row_count=len(rows),
        )
        return rows[:limit]

    def select(
        self, *, task_text: str, spec_limit: int = 6, symbol_limit: int = 6, test_limit: int = 6
    ) -> dict[str, Any]:
        """按任务文本检索三类资料，返回上下文候选集合。"""

        keywords = extract_keywords(task_text)
        return {
            "task": task_text,
            "keywords": keywords,
            "specs": self._search_specs(keywords, spec_limit),
            "symbols": self._search_symbols(keywords, symbol_limit),
            "tests": self._search_tests(keywords, test_limit),
        }

    def counts(self) -> dict[str, int]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                out: dict[str, int] = {}
                for table in ("spec_items", "code_symbols", "test_cases", "import_runs", "query_log"):
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    out[table] = int(cur.fetchone()[0])
        finally:
            conn.close()
        return out
