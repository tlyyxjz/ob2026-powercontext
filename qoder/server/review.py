"""点评生成：优先让模型写三段话，没配 Key / 失败 / 超时全部降级到 seekdb 规则点评。

- 配置只从环境变量读：LLM_BASE_URL、LLM_API_KEY（未设则回退 DEEPSEEK_API_KEY）、LLM_MODEL
- 只依赖标准库 urllib，不引入 openai SDK
- API Key 只在发请求那一行进入内存，不写文件、不进日志、不进响应体；对外只出现模型名
- 得分一律由 seekdb 的要点命中算出，模型只负责写字，不参与打分
- 本文件不碰数据库，也不出现 SQL；题库要点与历史作答由调用方从 db/store.py 取好传进来

用法见 server/app.py 的 /api/submit。
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
TIMEOUT = 8            # 秒：超过就降级，不能让浏览器一直转圈
MAX_TOTAL = 120        # 三段合计字数上限（题面要求 ≤120）
MAX_HISTORY = 3        # 最多带几次历史作答，避免 prompt 无限膨胀
MARKERS = {"hit": "你答到的点：", "missed": "你漏掉的点：", "advice": "一句话建议："}
KEY_FALLBACKS = ("LLM_API_KEY", "DEEPSEEK_API_KEY")
UNAVAILABLE = "点评暂时不可用，这次作答已经记下来了"


def config():
    """每次调用都重读环境变量，方便在同一个进程里演示「有 Key / 没 Key」两条路径。"""
    key, key_env = "", None
    for name in KEY_FALLBACKS:
        val = (os.environ.get(name) or "").strip()
        if val:
            key, key_env = val, name
            break
    base = (os.environ.get("LLM_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    model = (os.environ.get("LLM_MODEL") or "").strip() or DEFAULT_MODEL
    return {"base_url": base.rstrip("/"), "model": model, "api_key": key, "key_env": key_env}


def public(cfg):
    """给日志和响应用的脱敏视图：只有模型名与配置来源，没有任何密钥。"""
    return {
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "key_env": cfg["key_env"],
        "configured": bool(cfg["api_key"]),
    }


def scrub(text, cfg):
    """兜底：任何往外抛的文本里如果混进了密钥，一律先擦掉。

    擦两种：完整的 Key 原值，以及上游可能回显的 sk- 形态 token。
    """
    out = str(text)
    if cfg.get("api_key"):
        out = out.replace(cfg["api_key"], "***")
    return re.sub(r"sk-[A-Za-z0-9_-]{6,}", "***", out)


def clip(text, n):
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def _head(text, n):
    """列举用的短名：截断不加省略号，免得「+…」这种尾巴读着别扭。"""
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[:n].rstrip(" ，、；")


def _short_point(content, n=16):
    """要点原文太长，取第一个语义段做列举。"""
    for sep in ("，", "、", "；", "（", " "):
        if sep in content:
            head = content.split(sep)[0].strip()
            if len(head) >= 4:
                content = head
                break
    return _head(content, n)


def rule_sections(match_res):
    """纯规则三段：完全由 seekdb 的要点命中结果拼出来，不依赖任何模型。"""
    hit, missed = match_res["hit"], match_res["missed"]
    hit_text = "、".join(_short_point(p["content"]) for p in hit[:4]) if hit else "一个都没命中"
    missed_text = "、".join(_short_point(p["content"]) for p in missed[:3]) if missed else "全部覆盖"
    if match_res["total"] == 0:
        advice = "这道题没有参考要点，先跳过它"
    elif not hit:
        advice = "先照题目关键词把主线说一遍，再谈细节"
    elif not missed:
        advice = "要点全覆盖，补一句代价或适用边界就更稳"
    else:
        advice = "下一条先补：" + _short_point(missed[0]["content"], 24)
    return {"hit": hit_text, "missed": missed_text, "advice": advice}


def render(sections):
    return "\n".join(f"{MARKERS[k]}{sections[k]}" for k in ("hit", "missed", "advice"))


def fit_length(sections):
    """硬卡 120 字（按最终 text 计，含三个前缀与两个换行）：超了就一直压最长的那一段。"""
    sec = dict(sections)
    while len(render(sec)) > MAX_TOTAL:
        longest = max(sec, key=lambda k: len(sec[k]))
        sec[longest] = clip(sec[longest], len(sec[longest]) - 1)
    return sec


def build_prompt(question, answer_text, history, match_res):
    points = sorted(
        [{"no": p["point_no"], "c": p["content"], "hit": p in match_res["hit"]}
         for p in match_res["hit"] + match_res["missed"]],
        key=lambda x: x["no"],
    )
    lines = [f"题干：{question['stem']}", "参考答案要点："]
    lines += [f"{i + 1}. {p['c']}" for i, p in enumerate(points)]
    lines.append(f"本次回答：{clip(answer_text, 400)}")
    prior = [h for h in (history or []) if h["attempt_no"] != question.get("attempt_no")]
    prior = prior[-MAX_HISTORY:]
    if prior:
        lines.append("这个用户在这道题之前的作答：")
        lines += [f"第{h['attempt_no']}次（{h['score']}分）：{clip(h['answer_text'], 120)}" for h in prior]
    else:
        lines.append("这个用户在这道题之前没有作答记录。")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "你是数据库面试教练。根据题干、参考答案要点、学员本次回答和他在同一道题上的历史作答，"
    "只输出三行，依次以「你答到的点：」「你漏掉的点：」「一句话建议：」开头。"
    "三行合计不超过 120 字；只写要点名称，不解释、不客套、不用 markdown、不复述题干。"
    "如果这次漏的点跟上次一样，直接点出他重复犯同一个错。"
)


def call_model(cfg, prompt):
    """返回 (原文, 耗时ms)。失败抛 LlmError，带机器可读的 reason。"""
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 240,
        "stream": False,
    }
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # 上游报错文本里会带 Key 的残片，这里只出固定文案，不透传原文
            raise LlmError("llm_unauthorized", f"模型 Key 无效，HTTP {exc.code}") from exc
        detail = scrub(exc.read().decode("utf-8", "replace")[:200], cfg)
        raise LlmError("llm_error", f"HTTP {exc.code} {detail}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise LlmError("llm_timeout", f"请求超过 {TIMEOUT}s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise LlmError("llm_timeout", f"请求超过 {TIMEOUT}s") from exc
        raise LlmError("llm_unreachable", scrub(exc.reason, cfg)) from exc
    except (ValueError, KeyError) as exc:
        raise LlmError("llm_bad_json", scrub(exc, cfg)) from exc
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    try:
        text = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError("llm_bad_shape", scrub(exc, cfg)) from exc
    return text, elapsed_ms, body.get("usage") or {}


class LlmError(RuntimeError):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def parse_sections(raw):
    """按三个前缀切；模型少写某一段就用规则版补上，保证永远是三段。"""
    lines = [ln.strip() for ln in (raw or "").replace("\r", "").split("\n") if ln.strip()]
    found = {}
    key, buf = None, []
    for ln in lines:
        hit = next((k for k, m in MARKERS.items() if ln.startswith(m)), None)
        if hit:
            if key:
                found[key] = " ".join(buf).strip()
            key, buf = hit, [ln[len(MARKERS[hit]):].strip()]
        elif key:
            buf.append(ln)
        elif len(ln) < 60:      # 没打前缀的第一行当成建议
            key, buf = "advice", [ln]
    if key:
        found[key] = " ".join(buf).strip()
    return {k: (found.get(k) or "") for k in MARKERS}


def generate(question, match_res, history, answer_text):
    """(score, review)。

    调用方负责：question / match_res / history 都从 db/store.py 取，本函数只做「拼 prompt ->
    调模型 -> 校验 -> 兜底」，不碰数据库。模型失败也要返回可写库的规则版，产品不能因为没网就断。
    """
    total = match_res["total"]
    score = round(100 * len(match_res["hit"]) / total) if total else 0
    rule = rule_sections(match_res)
    review = {
        "hit": match_res["hit"],
        "missed": match_res["missed"],
        "total": total,
        "sections": dict(rule),
        "advice": rule["advice"],
        "text": render(rule),
        "engine": "seekdb-rule",
        "model": None,
        "note": None,
        "rule_reason": None,
        "elapsed_ms": None,
        "prompt_chars": None,
    }
    cfg = config()
    review["model_config"] = public(cfg)
    if not cfg["api_key"]:
        review["rule_reason"] = "no_key"
        return score, review

    prompt = build_prompt(question, answer_text, history, match_res)
    review["prompt_chars"] = len(prompt)
    try:
        raw, elapsed_ms, usage = call_model(cfg, prompt)
    except LlmError as exc:
        review["rule_reason"] = exc.reason
        review["note"] = f"{UNAVAILABLE}（{exc.detail}）"
        return score, review
    parsed = parse_sections(raw)
    if not parsed["hit"] or not parsed["missed"] or not parsed["advice"]:
        review["rule_reason"] = "llm_bad_format"
        review["note"] = f"{UNAVAILABLE}（模型没按三段格式回答）"
        return score, review
    sections = fit_length(parsed)
    review.update(
        engine=f"model:{cfg['model']}",
        model=cfg["model"],
        sections=sections,
        advice=sections["advice"],
        text=render(sections),
        elapsed_ms=elapsed_ms,
        raw_model_text=clip(raw, 400),
        token_usage=usage,
    )
    return score, review
