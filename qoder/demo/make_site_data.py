"""把 demo/queries.json（本地 seekdb 实际执行后导出的记录）内联成 web/site/queries.js。

站点要能在 file:// 下直接打开，fetch 跨目录会被 CORS 拦，所以内联成 window.QUERIES。
用法：python demo/make_site_data.py
"""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "demo" / "queries.json"
DST = ROOT / "web" / "site" / "queries.js"

HEADER = (
    "// 由 demo/queries.json 逐字内联而来，内容是本地 seekdb 实际执行后导出的检索记录（不是前端模拟）。\n"
    "// 重新生成：python demo/make_site_data.py\n"
)


def main():
    data = json.loads(SRC.read_text(encoding="utf-8"))
    need = {"generated_by", "engine", "ratio", "corpus", "cases"}
    missing = need - set(data)
    if missing:
        raise SystemExit(f"{SRC.name} 缺字段：{sorted(missing)}")
    if not data["cases"]:
        raise SystemExit(f"{SRC.name} 的 cases 是空的")
    body = json.dumps(data, ensure_ascii=False, indent=2)
    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(f"{HEADER}window.QUERIES = {body};\n", encoding="utf-8")
    print(f"{SRC.name}: {len(data['cases'])} 条记录 -> {DST.relative_to(ROOT)}（{DST.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
