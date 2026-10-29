"""P2 数据层自检：a 行数 / b 中文全文命中 / c 写入后当前得分 / d 关库重开后再查 c。
外加检索词行覆盖率 >50% 的停用词警告。每项都打印真实读数，不做「已完成」式汇报。

用法：python db/check.py            # 默认在 .data/_check 上追加跑，保留上一次的现场
      python db/check.py --fresh    # 先清空自检库再跑
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from init import bootstrap, load_questions  # noqa: E402
from store import SIMILAR_KEEP_RATIO, SeekdbStore  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECK_DIR = ROOT / ".data" / "_check"
PROBE_QID = 2  # 「什么是回表？」


def hr(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def reopen_probe(qid, db_dir):
    """子进程重开同一个库目录，证明数据真落盘。"""
    with SeekdbStore(db_dir) as store:
        cur = store.current_score(qid)
        print(f"[子进程] 重新打开 {pathlib.Path(db_dir).name} 后 question_id={qid} 的当前得分:")
        print(f"         score={cur['score']}  attempt_no={cur['attempt_no']}  answered_at={cur['answered_at']}")
        print(f"[子进程] 该题全部作答记录:")
        for a in store.attempts(qid):
            print(f"         #{a['attempt_no']} score={a['score']} answered_at={a['answered_at']} answer={a['answer_text'][:24]!r}")
        print(f"[子进程] stats={store.stats()}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="seekdb data-layer self check")
    ap.add_argument("--db-dir", default=str(CHECK_DIR))
    ap.add_argument("--fresh", action="store_true", help="先清空自检库（默认保留，追加验证跨进程持久化）")
    ap.add_argument("--probe", type=int, metavar="QID", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.probe is not None:
        reopen_probe(args.probe, args.db_dir)
        return 0

    db_dir = pathlib.Path(args.db_dir)
    if args.fresh and db_dir.exists():
        shutil.rmtree(db_dir)
        print(f"[准备] 已清空旧自检库 {db_dir}")
    print(f"[准备] 自检库目录 = {db_dir}")

    with SeekdbStore(db_dir) as store:
        hr("0) 幂等性：bootstrap 连跑两次")
        stmts, n = bootstrap(store)
        after1 = store.stats()
        stmts, n = bootstrap(store)
        after2 = store.stats()
        print(f"   第1次: schema {stmts} 条语句, 导入 {n} 题 -> {after1}")
        print(f"   第2次: schema {stmts} 条语句, 导入 {n} 题 -> {after2}")
        same = after1["questions"] == after2["questions"] and after1["ref_points"] == after2["ref_points"]
        print(f"   重复导入是否产生重复行: {'否（幂等成立）' if same else '是（幂等失败）'}")

        hr("a) 初始化后行数")
        st = store.stats()
        print(f"   questions = {st['questions']} 行")
        print(f"   ref_point = {st['ref_points']} 行")
        print(f"   answer_log = {st['answers']} 行")

        hr("b) 中文全文检索（索引列 = questions.keywords, WITH PARSER ik）")
        for kw in ("索引", "回表", "隔离级别", "分页"):
            sql = ("SELECT id, `level`, MATCH(keywords) AGAINST(%s) AS rel FROM questions"
                   " WHERE MATCH(keywords) AGAINST(%s) ORDER BY rel DESC")
            rows = store.query(sql, (kw, kw))
            print(f"   AGAINST('{kw}') 命中 {len(rows)} 行: "
                  + ", ".join(f"题{r['id']}={r['rel']:.6f}" for r in rows))

        hr("c) 写两条作答记录后，题 %d 的当前得分" % PROBE_QID)
        a1 = store.record_answer(PROBE_QID, "就是二级索引要再查一次主键，别的不知道", 35, "规则点评")
        a2 = store.record_answer(PROBE_QID, "二级索引叶子存主键，要额外列得回聚簇索引，覆盖索引能省掉这次回表", 72, "规则点评")
        cur = store.current_score(PROBE_QID)
        print(f"   写入 attempt_no={a1} (score 35) 与 attempt_no={a2} (score 72)")
        print(f"   current_score({PROBE_QID}) = {cur}")

        hr("c2) find_similar(题%d)：keywords 全文召回 + %.2f 相对阈值" % (PROBE_QID, SIMILAR_KEEP_RATIO))
        raw, _ = store.find_similar(PROBE_QID, limit=20, keep_ratio=0.0)
        rows, how = store.find_similar(PROBE_QID, limit=5)
        top = max((r["rel"] for r in raw), default=0)
        print(f"   召回路径 = {how}")
        print(f"   未剪枝 {len(raw)} 行 -> top={top:.6f} -> 阈值 top*{SIMILAR_KEEP_RATIO:g}"
              f"={top * SIMILAR_KEEP_RATIO:.6f} -> 保留 {len(rows)} 行")
        kept = {r["id"] for r in rows}
        for r in raw:
            mark = "保留" if r["id"] in kept else "剪掉"
            print(f"   [{mark}] 题{r['id']:<3} rel={r['rel']:<20.6f} 我当前得分={r['current_score']!s:<5}"
                  f" 次数={r['attempt_no']}  {r['stem'][:24]}")

        hr("c3) match_ref_points(题%d)：用户原话 vs 参考要点（按参考答案归一）" % PROBE_QID)
        good = "二级索引叶子存主键，要额外列得回聚簇索引，覆盖索引能省掉这次回表"
        vague = "索引就是让查询快一点吧，别的我记不清了"
        for label, text in (("答得全", good), ("说空话", vague)):
            res = store.match_ref_points(PROBE_QID, text)
            print(f"   {label}：要点 {res['total']} 个 -> 答到 {len(res['hit'])} / 漏掉 {len(res['missed'])}"
                  f" -> 得分 {round(100 * len(res['hit']) / res['total'])}")
            for p in res["hit"]:
                print(f"     [答到] #{p['point_no']} ratio={p['ratio']:.3f} rel={p['rel']:.4f} {p['content'][:30]}")
            for p in res["missed"]:
                print(f"     [漏掉] #{p['point_no']} ratio={p['ratio']:.3f} rel={p['rel']:.4f} {p['content'][:30]}")

        hr("c4) daily_plan(n=3, skip_recent_hours=0)：今日题单")
        for r in store.daily_plan(n=3, skip_recent_hours=0):
            print(f"   题{r['id']:<3} rank_score={r['rank_score']!s:<5} cur={r['cur_score']!s:<5} 次数={r['attempt_no'] or 0}  {r['stem'][:26]}")

        hr("e) 检索词行覆盖率检查（>50% 会被自然语言模式当停用词忽略）")
        cov = store.keyword_coverage()
        over = [c for c in cov if c["ratio"] > 0.5]
        total_terms = len(cov)
        top5 = sorted(cov, key=lambda c: -c["ratio"])[:5]
        print(f"   词表大小 = {total_terms}，最高覆盖率的 5 个词:")
        for c in top5:
            print(f"     {c['term']:<8} 覆盖 {c['rows']}/{c['total']} 行 = {c['ratio'] * 100:.1f}%")
        if over:
            print(f"   !! 警告：{len(over)} 个词覆盖率 > 50%，这些词在自然语言模式下会静默失效: "
                  + ", ".join(c["term"] for c in over))
        else:
            print(f"   无词超过 50% 覆盖率（当前 {len(cov) and top5[0]['rows']}/{top5[0]['total']} 行为上限）")

    hr("d) 关库后由全新进程重开同一目录再查 c")
    print(f"   父进程已 close，当前 answer_log 行数在子进程里重新读取：")
    sys.stdout.flush()
    subprocess.run(
        [sys.executable, str(pathlib.Path(__file__).resolve()), "--probe", str(PROBE_QID),
         "--db-dir", str(db_dir)],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
