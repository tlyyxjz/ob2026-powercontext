"""初始化 seekdb 库并导入题库。幂等：重复执行不报错、不产生重复行。

用法：python db/init.py [--db-dir PATH]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from store import DEFAULT_DB_DIR, ROOT, TAG_SEPARATOR, SeekdbStore  # noqa: E402

QUESTIONS_JSON = ROOT / "data" / "questions.json"


def load_questions(path=QUESTIONS_JSON):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def import_questions(store, items):
    """questions 一行一题，ref_points 数组拆成 ref_point 多行。"""
    for q in items:
        store.execute(
            "INSERT INTO questions (id, stem, keywords, `level`, tags)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE stem = VALUES(stem), keywords = VALUES(keywords),"
            " `level` = VALUES(`level`), tags = VALUES(tags)",
            (q["id"], q["stem"], q["keywords"], q["level"], TAG_SEPARATOR.join(q["tags"])),
        )
        for no, point in enumerate(q["ref_points"], 1):
            store.execute(
                "INSERT INTO ref_point (question_id, point_no, content)"
                " VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE content = VALUES(content)",
                (q["id"], no, point),
            )
    return len(items)


def bootstrap(store, items=None):
    """建表 + 导入，返回 schema 语句数与题数。可重复调用。"""
    stmts = store.apply_schema()
    n = import_questions(store, items if items is not None else load_questions())
    return stmts, n


def main(argv=None):
    ap = argparse.ArgumentParser(description="init seekdb db and import question bank")
    ap.add_argument("--db-dir", default=str(DEFAULT_DB_DIR))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    with SeekdbStore(args.db_dir) as store:
        stmts, n = bootstrap(store)
        st = store.stats()
        if not args.quiet:
            print(f"db_dir      : {store.db_dir}")
            print(f"schema      : {stmts} statements applied (CREATE TABLE IF NOT EXISTS)")
            print(f"questions   : imported {n} rows -> now {st['questions']}")
            print(f"ref_point   : now {st['ref_points']} rows")
            print(f"answer_log  : now {st['answers']} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
