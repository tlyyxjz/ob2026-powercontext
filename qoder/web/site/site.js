// 回放区渲染。数据只读 window.QUERIES（由 demo/queries.json 内联而来，
// 是本地 seekdb 实际执行后导出的记录）。这个文件不做任何检索、不造数据。
(function () {
  const Q = window.QUERIES;
  const box = document.getElementById("cases");
  if (!Q || !Array.isArray(Q.cases)) return;

  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };
  const num = (v, digits) => (typeof v === "number" ? v.toFixed(digits) : String(v));

  document.getElementById("factEngine").textContent = Q.engine || "—";
  document.getElementById("factCorpus").textContent =
    Q.corpus ? `题库 ${Q.corpus.questions} 题 / 要点 ${Q.corpus.ref_points} 条` : "—";
  document.getElementById("factRatio").textContent =
    Q.ratio ? `score ≥ top × ${Q.ratio}` : "—";
  document.getElementById("factCases").textContent = `${Q.cases.length} 条`;

  box.replaceChildren();

  Q.cases.forEach((c, i) => {
    const kept = new Set(c.kept || []);
    const card = node("article", "case");

    const head = node("div", "caseHead");
    head.appendChild(node("span", "from", "由本地 seekdb 实际执行后导出"));
    head.appendChild(node("span", null, `记录 ${i + 1} / ${Q.cases.length}`));
    head.appendChild(node("span", null, `表 ${Q.table || "questions"}`));
    card.appendChild(head);

    const say = node("p", "caseSay");
    say.appendChild(node("span", "who", "用户当时输入的原话"));
    say.appendChild(node("span", "what", `「${c.user_text}」`));
    const term = node("span", "term");
    term.appendChild(document.createTextNode("抽出检索词 "));
    term.appendChild(node("code", null, `AGAINST('${c.query}')`));
    say.appendChild(term);
    card.appendChild(say);

    const sql = node("div", "sqlWrap");
    sql.appendChild(node("span", "lbl", "实际执行的 SQL（原文）"));
    const pre = node("pre", "sql");
    const at = (c.sql || "").indexOf("AGAINST(");
    if (at < 0) {
      pre.textContent = c.sql || "(无)";
    } else {
      pre.appendChild(document.createTextNode(c.sql.slice(0, at)));
      pre.appendChild(node("span", "again", c.sql.slice(at)));
    }
    sql.appendChild(pre);
    card.appendChild(sql);

    const hits = node("div", "hits");
    hits.appendChild(node("span", "lbl", `命中 ${c.hits.length} 行 · 保留 ${c.kept.length} 行`
      + `（top=${num(c.top_score, 4)}，阈值=${num(c.threshold, 4)}）`));
    for (const h of c.hits) {
      const keep = kept.has(h.id);
      const row = node("div", "hitRow " + (keep ? "keep" : "cut"));
      row.appendChild(node("span", "hid", `题 ${h.id}`));
      row.appendChild(node("span", "stem", h.stem));
      row.appendChild(node("span", "sc", num(h.score, 4)));
      row.appendChild(node("span", "tag", keep ? "保留" : "低于阈值被剪掉"));
      hits.appendChild(row);
    }
    card.appendChild(hits);

    const foot = node("p", "caseFoot");
    foot.appendChild(node("span", null,
      `保留的题号：${c.kept.map((id) => `#${id}`).join(" ") || "无"}`));
    const cutIds = c.hits.filter((h) => !kept.has(h.id)).map((h) => `#${h.id}`);
    foot.appendChild(node("span", null,
      cutIds.length ? `剪掉 ${cutIds.length} 行：${cutIds.join(" ")}` : "没有噪声可剪"));
    card.appendChild(foot);

    box.appendChild(card);
  });
})();
