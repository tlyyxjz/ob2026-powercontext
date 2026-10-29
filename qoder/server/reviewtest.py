"""P4 点评自测：贴三组真实读数——有 Key（真调模型）、没 Key（纯规则降级）、Key 无效（错误态③）。

不打印任何密钥：全程只走 review.public()，最后再扫一遍输出确认没有 sk- 串。
用法：python server/reviewtest.py [--db-dir .data/selftest.db] [--question 2]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(ROOT / "server"))
from store import SeekdbStore  # noqa: E402
import review  # noqa: E402

GOOD = "二级索引叶子只存索引列和主键，要别的列得回聚簇索引查一次，覆盖索引能省掉这次回表；行数多的时候回表比全表扫还慢"
BAD = "索引就是让查询快一点吧，别的我记不清了"
FAKE_KEY = "sk-this-is-an-invalid-demo-key"


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_case(tag, store, qid, answer, collected):
    question = store.question(qid)
    match_res = store.match_ref_points(qid, answer)
    history = store.attempts(qid)
    print(f"[{tag}] 题干：{question['stem']}")
    print(f"[{tag}] 来自 ref_point 的参考要点：{match_res['total']} 条"
          f"（命中 {len(match_res['hit'])} / 漏 {len(match_res['missed'])}）")
    print(f"[{tag}] 来自 answer_log 的历史作答：{len(history)} 条 -> "
          + " · ".join(f"#{h['attempt_no']} {h['score']}分" for h in history))
    score, rev = review.generate(question, match_res, history, answer)
    print(f"[{tag}] engine={rev['engine']} model={rev['model']} "
          f"rule_reason={rev['rule_reason']} 得分={score} 耗时={rev['elapsed_ms']}ms")
    if rev["note"]:
        print(f"[{tag}] note：{rev['note']}")
    if rev["prompt_chars"] is not None:
        print(f"[{tag}] prompt 长度：{rev['prompt_chars']} 字符，token 用量：{rev.get('token_usage')}")
    if rev.get("raw_model_text"):
        print(f"[{tag}] 模型返回原文：")
        print("    " + rev["raw_model_text"].replace("\n", "\n    "))
    print(f"[{tag}] 最终三段（{len(rev['text'])} 字）：")
    for line in rev["text"].split("\n"):
        print(f"    {line}")
    collected.append((tag, score, rev))
    return rev


def main(argv=None):
    ap = argparse.ArgumentParser(description="review.py self check")
    ap.add_argument("--db-dir", default=str(ROOT / ".data" / "selftest.db"))
    ap.add_argument("--question", type=int, default=2)
    args = ap.parse_args(argv)

    collected = []
    store = SeekdbStore(args.db_dir)
    try:
        hr("A) 有 Key：真实调用模型（Key 从环境变量取，只报来源不报值）")
        cfg = review.config()
        print("A.1 生效配置（脱敏）:", review.public(cfg))
        print(f"A.2 Key 来源：{cfg['key_env']}（长度 {len(cfg['api_key'])}，前缀 {cfg['api_key'][:4]}…值不打印）")
        prompt = review.build_prompt(
            store.question(args.question), GOOD,
            store.attempts(args.question), store.match_ref_points(args.question, GOOD))
        print("A.3 request body 关键字段：")
        print("      POST", cfg["base_url"] + "/chat/completions")
        print("      model      =", cfg["model"])
        print("      temperature= 0.3   max_tokens= 240   stream= false   timeout= 8s")
        print("      messages   = [system, user]  user 长度 =", len(prompt), "字符")
        print("      system 内容：")
        for line in review.SYSTEM_PROMPT.split("\n"):
            print("        " + line)
        print("      user 内容（题干/要点/本次原话/历史作答，全部来自 seekdb）：")
        for line in prompt.split("\n"):
            print("        " + line)
        show_case("A", store, args.question, GOOD, collected)

        hr("B) 没 Key：清掉环境变量后同一条路（应退化成 seekdb 规则点评，且照样能写库）")
        saved = {k: os.environ.pop(k, None) for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY")}
        print("B.1 生效配置（脱敏）:", review.public(review.config()))
        show_case("B", store, args.question, BAD, collected)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

        hr("C) Key 无效：临时把 LLM_API_KEY 设成一个假 Key（错误态③的文案来源）")
        os.environ["LLM_API_KEY"] = FAKE_KEY
        print("C.1 生效配置（脱敏）:", review.public(review.config()), "（Key 是故意写错的演示值）")
        show_case("C", store, args.question, GOOD, collected)
        del os.environ["LLM_API_KEY"]
    finally:
        store.close()

    hr("D) 交付前自查")
    for tag, score, rev in collected:
        blob = json.dumps(rev, ensure_ascii=False, default=str)
        leak = "sk-" in blob
        print(f"  {tag}: engine={rev['engine']} score={score} "
              f"三段齐全={all(rev['sections'].values())} 长度={len(rev['text'])} 响应含密钥={leak}")
        if len(rev["text"]) > review.MAX_TOTAL:
            print(f"  !! {tag} 超过 {review.MAX_TOTAL} 字上限")
    print("\n结果：三组跑完；有 Key 的 engine 以 model: 开头、没 Key 的是 seekdb-rule、"
          "无效 Key 带「点评暂时不可用」提示且 rule_reason=llm_unauthorized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
