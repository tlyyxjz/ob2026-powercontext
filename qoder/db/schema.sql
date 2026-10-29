-- 复盘面试官 · seekdb 数据层 schema
-- 中文全文索引必须带 WITH PARSER ik；不带的话中文会被切成单字，检索结果全错。
-- ORGANIZATION = HEAP 是 seekdb 官方 README 里示例表的写法。

CREATE TABLE IF NOT EXISTS questions (
  id       INT PRIMARY KEY,
  stem     TEXT,
  keywords TEXT,
  `level`  INT,
  tags     TEXT,
  FULLTEXT INDEX idx_q_ft (keywords) WITH PARSER ik
) ORGANIZATION = HEAP;

-- ref_points 在 JSON 里是数组，拆成一行一个要点，MATCH 才能定位到「答到了第几点」。
CREATE TABLE IF NOT EXISTS ref_point (
  question_id INT,
  point_no    INT,
  content     TEXT,
  PRIMARY KEY (question_id, point_no),
  FULLTEXT INDEX idx_rp_ft (content) WITH PARSER ik
) ORGANIZATION = HEAP;

-- 复合主键代替 AUTO_INCREMENT：attempt_no 本来就是「该题第几次」，
-- 天然唯一，也让重复导入/重复提交不会长出新行。
CREATE TABLE IF NOT EXISTS answer_log (
  question_id INT,
  attempt_no  INT,
  answer_text TEXT,
  score       INT,
  comment     TEXT,
  answered_at DATETIME,
  PRIMARY KEY (question_id, attempt_no)
) ORGANIZATION = HEAP;
