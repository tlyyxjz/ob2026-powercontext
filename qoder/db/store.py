"""seekdb 数据层：题库检索、内容匹配、作答历史、今日题单。

数据层只有 seekdb（pylibseekdb 嵌入式 + pymysql），没有 sqlite / localStorage / Sites 数据库。
"""
from __future__ import annotations

import datetime
import pathlib

import pylibseekdb
import pymysql
from pymysql.cursors import DictCursor

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DB_DIR = ROOT / ".data" / "seekdb.db"
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent / "schema.sql"

# 相似题召回的相对阈值：得分低于 top * SIMILAR_KEEP_RATIO 的行丢掉。
# 实测 ik 会把「回表」切成单字「表」，导致「分库分表」的题也带进来，靠这个阈值剪掉。
# 0.3 是逐题跑 find_similar 按「top1 与本题 tag 有无交集」定的：21 题里同族命中 20/21；
# 0.5 会把索引族题的 题1(索引失效) / 题13(最左前缀) 误剪。
SIMILAR_KEEP_RATIO = 0.3

# questions.tags 在库里的存储口径：逗号分隔（"索引,执行计划"），不是竖线。
# 复算 tag 交集时必须按这个切，VERIFY.md 里要写同一条。
TAG_SEPARATOR = ","


# 要点命中的判定线：用户原话对该要点的命中分 / 参考答案对该要点的命中分 >= 0.25 才算答到。
# 分母让判定与「答案写了多长」无关。实测三档：空话 0 个命中、答对一半 2 个、答得全 3 个；
# 不做归一时空话和答全都能拿 75 分（相对阈值只跟本次最高分比，被泛用词灌满）。
GOLD_HIT_RATIO = 0.25


def split_tags(tags):
    return [t.strip() for t in (tags or "").split(TAG_SEPARATOR) if t.strip()]


class SeekdbStore:
    """一个 seekdb 库目录的连接封装；打开即启动嵌入式实例，close 即关停。"""

    def __init__(self, db_dir=None):
        self.db_dir = pathlib.Path(db_dir or DEFAULT_DB_DIR)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        # open() 每次可能换端口，connection_options 必须在 open 之后从实例上取
        self._inst = pylibseekdb.open(str(self.db_dir))
        self._conn = pymysql.connect(
            database="test",
            autocommit=True,
            cursorclass=DictCursor,
            **self._inst.connection_options(),
        )

    def close(self):
        self._conn.close()
        self._inst.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- 基础 ----------

    def query(self, sql, params=None):
        with self._conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

    def execute(self, sql, params=None):
        with self._conn.cursor() as cur:
            return cur.execute(sql, params or ())

    def count(self, table):
        return self.query(f"SELECT COUNT(*) AS c FROM {table}")[0]["c"]

    def apply_schema(self):
        """按 ; 切分执行 schema.sql；IF NOT EXISTS 已实测支持，可重复执行。"""
        raw = SCHEMA_PATH.read_text(encoding="utf-8")
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
        statements = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
        for stmt in statements:
            self.execute(stmt)
        return len(statements)

    # ---------- 作答历史 ----------

    def record_answer(self, question_id, answer_text, score, comment=None, answered_at=None):
        """记一次作答，attempt_no 自增为该题第几次。重复调用产生新行，不覆盖历史。"""
        row = self.query(
            "SELECT MAX(attempt_no) AS mx FROM answer_log WHERE question_id = %s",
            (question_id,),
        )[0]
        attempt_no = (row["mx"] or 0) + 1
        self.execute(
            "INSERT INTO answer_log (question_id, attempt_no, answer_text, score, comment, answered_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                question_id,
                attempt_no,
                answer_text,
                int(score),
                comment,
                answered_at or datetime.datetime.now().replace(microsecond=0),
            ),
        )
        return attempt_no

    def latest_scores(self):
        """每题最新一次得分。一条 SQL 拿全表，不在循环里逐题查（这产品不该自己踩 N+1）。"""
        rows = self.query(
            "SELECT a.question_id AS id, a.score AS score, a.attempt_no AS attempt_no,"
            " a.answered_at AS answered_at"
            " FROM answer_log a"
            " JOIN (SELECT question_id, MAX(attempt_no) AS mx FROM answer_log GROUP BY question_id) t"
            " ON t.question_id = a.question_id AND t.mx = a.attempt_no"
        )
        return {r["id"]: r for r in rows}

    def current_score(self, question_id):
        hit = self.latest_scores().get(question_id)
        if hit is None:
            return {"score": None, "attempt_no": 0, "answered_at": None}
        return {k: hit[k] for k in ("score", "attempt_no", "answered_at")}

    def attempts(self, question_id):
        return self.query(
            "SELECT attempt_no, answer_text, score, comment, answered_at FROM answer_log"
            " WHERE question_id = %s ORDER BY attempt_no",
            (question_id,),
        )

    # ---------- 题库读取 ----------

    def question(self, question_id):
        rows = self.query(
            "SELECT id, stem, keywords, `level`, tags FROM questions WHERE id = %s", (question_id,)
        )
        if not rows:
            return None
        r = dict(rows[0])
        r["tags_list"] = split_tags(r["tags"])
        r["ref_point_count"] = self.query(
            "SELECT COUNT(*) AS c FROM ref_point WHERE question_id = %s", (question_id,)
        )[0]["c"]
        cur = self.current_score(question_id)
        r.update(
            current_score=cur["score"], attempt_no=cur["attempt_no"], answered_at=cur["answered_at"]
        )
        r["history"] = self.query(
            "SELECT attempt_no, score, answered_at FROM answer_log"
            " WHERE question_id = %s ORDER BY attempt_no",
            (question_id,),
        )
        return r

    def wrong_book(self, max_score=60):
        """错题本：最新一次得分低于阈值的题，按得分升序。"""
        return self.query(
            "SELECT q.id, q.stem, q.`level`, a.score AS current_score, a.attempt_no, a.answered_at"
            " FROM questions q"
            " JOIN answer_log a ON a.question_id = q.id"
            " JOIN (SELECT question_id, MAX(attempt_no) AS mx FROM answer_log GROUP BY question_id) t"
            " ON t.question_id = a.question_id AND t.mx = a.attempt_no"
            " WHERE a.score < %s"
            " ORDER BY a.score ASC, a.answered_at ASC",
            (max_score,),
        )

    def count_answers_since(self, since):
        return self.query(
            "SELECT COUNT(*) AS c FROM answer_log WHERE answered_at >= %s", (since,)
        )[0]["c"]

    # ---------- 相似题（seekdb 的核心做功处）----------

    def find_similar(self, question_id, limit=5, exclude_recent_hours=None, keep_ratio=None):
        """用源题 keywords 做中文全文召回，带相对阈值剪枝，并附上我在每题的当前得分。

        keep_ratio 传 0 可以拿到未剪枝的原始召回，自检和调阈值时用。
        返回 (rows, how)：how 为 'fulltext' 或 'tag_fallback'。
        """
        ratio = SIMILAR_KEEP_RATIO if keep_ratio is None else keep_ratio
        src = self.query("SELECT keywords, tags FROM questions WHERE id = %s", (question_id,))
        if not src:
            return [], "unknown_question"
        keywords = src[0]["keywords"] or ""
        sql = (
            "SELECT id, stem, `level`, tags, MATCH(keywords) AGAINST(%s) AS rel"
            " FROM questions"
            " WHERE id <> %s AND MATCH(keywords) AGAINST(%s)"
            " ORDER BY rel DESC"
        )
        rows = self.query(sql, (keywords, question_id, keywords))
        how = "fulltext"
        if not rows:
            rows = self._similar_by_tags(question_id, src[0]["tags"])
            how = "tag_fallback"
        if rows:
            top = max(r["rel"] for r in rows)
            rows = [r for r in rows if r["rel"] >= top * ratio]
        scores = self.latest_scores()
        out = []
        for r in rows:
            cur = scores.get(r["id"])
            out.append(
                {
                    "id": r["id"],
                    "stem": r["stem"],
                    "level": r["level"],
                    "rel": r["rel"],
                    "current_score": cur["score"] if cur else None,
                    "attempt_no": cur["attempt_no"] if cur else 0,
                    "answered_at": cur["answered_at"] if cur else None,
                }
            )
        if exclude_recent_hours:
            cutoff = datetime.datetime.now() - datetime.timedelta(hours=exclude_recent_hours)
            out = [r for r in out if r["answered_at"] is None or r["answered_at"] < cutoff]
        return out[:limit], how

    def _similar_by_tags(self, question_id, tags):
        """兜底：全文零命中时按 tags 重叠召回，rel 记 0，产品不至于退化成随机出题。"""
        terms = split_tags(tags)
        if not terms:
            return []
        cond = " OR ".join(["tags LIKE %s"] * len(terms))
        rows = self.query(
            f"SELECT id, stem, `level`, tags, 0 AS rel FROM questions WHERE id <> %s AND ({cond})",
            [question_id] + [f"%{t}%" for t in terms],
        )
        return list(rows)

    # ---------- 内容匹配：答到了哪几个要点 ----------

    def _gold_scores(self, points):
        """参考答案（全部要点拼起来）对每个要点自身的命中分，作为归一分母。"""
        gold = " ".join(p["content"] for p in points)
        rows = self.query(
            "SELECT point_no, MATCH(content) AGAINST(%s) AS rel FROM ref_point"
            " WHERE question_id = %s",
            (gold, points[0]["question_id"]),
        )
        return {r["point_no"]: r["rel"] for r in rows}

    def match_ref_points(self, question_id, answer_text):
        """把用户原话和该题的参考要点逐条比，返回答到的 / 漏掉的。

        每个要点按参考答案自身的命中分归一，命中线与答案长度无关。
        """
        points = self.query(
            "SELECT question_id, point_no, content FROM ref_point WHERE question_id = %s"
            " ORDER BY point_no",
            (question_id,),
        )
        if not points:
            return {"hit": [], "missed": [], "total": 0}
        den = self._gold_scores(points)
        rel = {
            r["point_no"]: r["rel"]
            for r in self.query(
                "SELECT point_no, MATCH(content) AGAINST(%s) AS rel FROM ref_point"
                " WHERE question_id = %s AND MATCH(content) AGAINST(%s)"
                " ORDER BY rel DESC",
                (answer_text, question_id, answer_text),
            )
        }
        hit, missed = [], []
        for p in points:
            d = den.get(p["point_no"], 0.0)
            r = rel.get(p["point_no"], 0.0)
            ratio = r / d if d else 0.0
            item = {"point_no": p["point_no"], "content": p["content"], "rel": r, "ratio": ratio}
            (hit if ratio >= GOLD_HIT_RATIO else missed).append(item)
        hit.sort(key=lambda x: -x["ratio"])
        return {"hit": hit, "missed": missed, "total": len(points)}

    # ---------- 今日题单 ----------

    def daily_plan(self, n=3, skip_recent_hours=24, max_level=None):
        """错题优先：没练过的视作 100 分（不是错题），练过但分低的排前面。"""
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=skip_recent_hours)
        where = ["(a.answered_at IS NULL OR a.answered_at < %s)"]
        params = [cutoff]
        if max_level is not None:
            where.append("`level` <= %s")
            params.append(max_level)
        sql = (
            "SELECT q.id, q.stem, q.`level`, q.tags, a.score AS cur_score, a.attempt_no,"
            " a.answered_at, IFNULL(a.score, 100) AS rank_score"
            " FROM questions q"
            " LEFT JOIN (SELECT l.question_id, l.score, l.attempt_no, l.answered_at FROM answer_log l"
            "   JOIN (SELECT question_id, MAX(attempt_no) AS mx FROM answer_log GROUP BY question_id) t"
            "   ON t.question_id = l.question_id AND t.mx = l.attempt_no) a"
            " ON a.question_id = q.id"
            " WHERE " + " AND ".join(where) +
            " ORDER BY rank_score ASC, a.answered_at ASC, q.id ASC LIMIT %s"
        )
        return self.query(sql, params + [n])

    # ---------- 状态与自检辅助 ----------

    def stats(self):
        latest = self.latest_scores()
        return {
            "questions": self.count("questions"),
            "ref_points": self.count("ref_point"),
            "answers": self.count("answer_log"),
            "practiced_questions": len(latest),
            "wrong_book": sum(1 for r in latest.values() if r["score"] < 60),
            "avg_latest_score": (
                round(sum(r["score"] for r in latest.values()) / len(latest), 1) if latest else None
            ),
        }

    def keyword_coverage(self):
        """每个检索词覆盖了多少行。>50% 的词在自然语言模式下会被当成停用词忽略（实测当前 12 行未触发）。"""
        total = self.count("questions")
        if not total:
            return []
        terms = set()
        for row in self.query("SELECT keywords FROM questions"):
            terms.update(t for t in (row["keywords"] or "").split() if t)
        out = []
        for t in sorted(terms):
            n = self.query(
                "SELECT COUNT(*) AS c FROM questions WHERE keywords LIKE %s", (f"%{t}%",)
            )[0]["c"]
            out.append({"term": t, "rows": n, "total": total, "ratio": n / total})
        return out

    def search(self, text, limit=10):
        """按一段任意中文检索题库（给 check.py 和知识库检索入口用）。"""
        return self.query(
            "SELECT id, stem, `level`, MATCH(keywords) AGAINST(%s) AS rel FROM questions"
            " WHERE MATCH(keywords) AGAINST(%s) ORDER BY rel DESC LIMIT %s",
            (text, text, limit),
        )
