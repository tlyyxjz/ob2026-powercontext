// 复盘面试官 · 前端。无构建工具、无 npm、无 CDN，全部走 /api/* 拿 seekdb 里的数据。
const LEVEL = { 1: "基础", 2: "进阶", 3: "硬核" };
const GOAL = 3;

const el = (id) => document.getElementById(id);
const ui = {
  goal: el("goal"), start: el("startBtn"), wrongBtn: el("wrongBtn"), wrongCount: el("wrongCount"),
  empty: el("emptyState"), emptyLead: el("emptyLead"), emptyStart: el("emptyStart"),
  busy: el("busyBar"), busyText: el("busyText"),
  card: el("qCard"), review: el("reviewCard"), error: el("errorBar"),
  qId: el("qId"), qLevel: el("qLevel"), qTags: el("qTags"), qScore: el("qScore"),
  stem: el("qStem"), attempts: el("qAttempts"),
  form: el("answerForm"), box: el("answerBox"), submit: el("submitBtn"), writeStatus: el("writeStatus"),
  revEngine: el("revEngine"), revNote: el("revNote"), revScore: el("revScore"),
  hit: el("hitList"), miss: el("missList"),
  advice: el("adviceText"), next: el("nextBtn"), retry: el("retryBtn"),
  drawer: el("wrongDrawer"), drawerClose: el("drawerClose"), wrongList: el("wrongList"), wrongMax: el("wrongMax"),
  dot: el("dbDot"), dbText: el("dbText"), llmChip: el("llmChip"),
};

let current = null;   // 当前题目（/api/question 的返回）
let busy = false;

// 后端回来的错误带 code，三种库/模型故障才分得出文案
const FAULT_TEXT = {
  db_not_initialized: "① 数据目录没初始化：这个库里还没有题库表。",
  db_locked: "② 库文件被占用：seekdb 实例起不来（目录不可写，或别的进程正占着它）。",
  llm_unauthorized: "③ 模型 Key 无效：LLM_API_KEY / DEEPSEEK_API_KEY 被服务端拒了。",
};

function describe(err) {
  if (err.code && FAULT_TEXT[err.code]) {
    return { title: FAULT_TEXT[err.code], hint: err.hint || "", detail: err.message };
  }
  if (err.noBackend) {
    return {
      title: "② 库文件被占用 / 后端没起：连不上本机服务。",
      hint: "起服务：python server/app.py（默认 8765，可用 --port 换）",
      detail: err.message,
    };
  }
  return { title: err.message, hint: "", detail: "" };
}

async function request(path, options) {
  let r;
  try {
    r = await fetch(path, options);
  } catch (exc) {
    const err = new Error(`请求 ${path} 失败：${exc.message}`);
    err.noBackend = true;
    throw err;
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.ok === false) {
    const err = new Error(data.error || `HTTP ${r.status}`);
    err.code = data.code;
    err.hint = data.hint;
    throw err;
  }
  return data;
}

const get = (path) => request(path);
const post = (path, body) => request(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// ---------- 加载态：出题 / 交卷写库 / 生成点评，三个动作都要看得见在跑 ----------

function setBusy(text) {
  busy = !!text;
  ui.busy.hidden = !text;
  if (text) ui.busyText.textContent = text;
  ui.start.disabled = busy;
  ui.emptyStart.disabled = busy;
  ui.next.disabled = busy || !current;
  ui.submit.disabled = busy || !ui.box.value.trim();
}

function say(message, info) {
  ui.error.replaceChildren();
  ui.error.hidden = !message;
  if (!message) return;
  ui.error.appendChild(document.createTextNode(message));
  if (info && info.hint) {
    const line = document.createElement("span");
    line.className = "hintline";
    line.textContent = "修法：" + info.hint;
    ui.error.appendChild(line);
  }
}

function fail(err, prefix) {
  const d = describe(err);
  say(`${prefix}${d.title}`, d);
  if (d.detail && d.detail !== d.title) {
    const line = document.createElement("span");
    line.className = "hintline";
    line.textContent = d.detail;
    ui.error.appendChild(line);
  }
}

// ---------- 状态条：给评审看的，连没连上、库里多少条、点评走哪条路，都要看得见 ----------

async function refreshStats() {
  try {
    const data = await get("/api/stats");
    const s = data.stats;
    ui.dot.className = "dot on";
    ui.dbText.textContent =
      `seekdb 已连接 · 题库 ${s.questions} 题 / 要点 ${s.ref_points} 条 · 作答记录 ${s.answers} 条` +
      ` · 错题 ${s.wrong_book} · 相似阈值 ${data.keep_ratio} · 命中线 ${data.gold_ratio}`;
    ui.wrongCount.textContent = s.wrong_book;
    const llm = data.llm || {};
    ui.llmChip.className = "llmChip " + (llm.configured ? "model" : "rule");
    ui.llmChip.textContent = llm.configured
      ? `点评：${llm.model}（Key 来自 ${llm.key_env}，超时 8s 自动降级）`
      : "点评：本地规则（未配置 LLM_API_KEY，功能不受影响）";
    return s;
  } catch (err) {
    ui.dot.className = "dot off";
    const d = describe(err);
    ui.dbText.textContent = `seekdb 未连接：${d.title}`;
    ui.llmChip.className = "llmChip rule";
    ui.llmChip.textContent = "点评：不可用（后端未连接）";
    return null;
  }
}

async function loadPlan() {
  ui.goal.textContent = "出题中…";
  try {
    const data = await get(`/api/plan?n=${GOAL}`);
    ui.goal.textContent =
      `今天 ${data.goal} 题 · 已答 ${data.answered_today} · 还有 ${data.remaining_today} 道没过`;
    return data;
  } catch (err) {
    const d = describe(err);
    ui.goal.textContent = `读题单失败：${d.title}`;
    return null;
  }
}

// ---------- 空态：第一次进来要说清这地方记什么 ----------

function renderEmpty(stats) {
  const s = stats || ui.lastStats;
  if (!s) return;
  ui.emptyLead.textContent = s.answers === 0
    ? "你还没答过任何题。答完之后，它把「你漏了哪几个要点」写进 seekdb，"
      + "下次专门挑同一族、换了说法的题让你重练——这里记住的是你答崩的题，不是你做对的题。"
    : `已经记了 ${s.answers} 条作答，其中 ${s.wrong_book} 道题最新一次没到 60 分。`
      + "点「开始」接着练，或从错题本跳回答崩的那道。";
}

// ---------- 出题 ----------

function tagSpan(text, className) {
  const s = document.createElement("span");
  if (className) s.className = className;
  s.textContent = text;
  return s;
}

function noneItem(text) {
  const li = document.createElement("li");
  li.className = "none";
  li.textContent = text;
  return li;
}

function renderQuestion(q) {
  current = q;
  ui.empty.hidden = true;
  ui.card.hidden = false;
  ui.review.hidden = true;
  ui.revNote.hidden = true;
  ui.qId.textContent = `题 ${q.id}`;
  ui.qLevel.textContent = `难度 ${q.level} · ${LEVEL[q.level] || ""}`;
  ui.qTags.replaceChildren(...(q.tags_list || []).map((t) => tagSpan(`#${t}`)));
  const score = q.current_score;
  ui.qScore.textContent = score === null ? "未练过" : `当前 ${score} 分（第 ${q.attempt_no} 次）`;
  ui.qScore.className = "score" + (score === null ? "" : score < 60 ? " low" : " high");
  ui.stem.textContent = q.stem;
  ui.attempts.textContent = q.history && q.history.length
    ? "历次：" + q.history.map((h) => `#${h.attempt_no} ${h.score}分`).join(" · ")
    : "这是你第一次答这道题";
  ui.box.value = "";
  ui.submit.disabled = true;
  ui.writeStatus.textContent = "";
  ui.writeStatus.className = "writestatus";
  ui.form.hidden = false;
}

async function loadQuestion(id, reason) {
  say("");
  try {
    const data = await get(`/api/question?id=${id}`);
    renderQuestion(data.question);
    if (reason) ui.writeStatus.textContent = reason;
    ui.card.scrollIntoView({ block: "nearest" });
  } catch (err) {
    fail(err, "出题失败：");
  }
}

async function showQuestion(id, reason) {
  if (busy) return;
  setBusy("出题中…");
  try {
    await loadQuestion(id, reason);
  } finally {
    setBusy("");
    ui.box.focus();
  }
}

// ---------- 交卷：先落库成功，才许切点评 ----------

function renderReview(q, res) {
  const rev = res.review;
  ui.review.hidden = false;
  ui.revEngine.textContent = rev.engine + (rev.elapsed_ms ? ` · ${rev.elapsed_ms}ms` : "");
  ui.revNote.hidden = !rev.note;
  if (rev.note) ui.revNote.textContent = rev.note;
  const detail = document.createElement("small");
  detail.textContent =
    `分 · 第 ${res.attempt_no} 次作答 · seekdb 命中 ${rev.hit.length}/${rev.total} 个要点`
    + (rev.model ? ` · 模型 ${rev.model}` : " · 本地规则");
  ui.revScore.replaceChildren(String(res.score), detail);
  const fill = (list, items, emptyText) => {
    list.replaceChildren();
    if (!items.length) {
      list.appendChild(noneItem(emptyText));
      return;
    }
    for (const p of items) {
      const li = document.createElement("li");
      li.textContent = `${p.content}（命中 ${Math.round(p.ratio * 100)}%）`;
      list.appendChild(li);
    }
  };
  fill(ui.hit, rev.hit, "这次一个要点都没命中");
  fill(ui.miss, rev.missed, "全部要点都覆盖到了");
  ui.advice.textContent = rev.advice;
  ui.next.disabled = false;
}

ui.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!current || busy) return;
  const text = ui.box.value.trim();
  if (!text) return;
  setBusy("交卷：先写 seekdb，再生成点评…");
  ui.submit.textContent = "提交中…";
  ui.writeStatus.className = "writestatus";
  ui.writeStatus.textContent = "正在写库…";
  say("");
  try {
    const res = await post("/api/submit", { question_id: current.id, answer_text: text });
    ui.writeStatus.className = "writestatus good";
    ui.writeStatus.textContent = `已写入 seekdb（第 ${res.attempt_no} 次，${res.answered_at}）`;
    current.current_score = res.score;
    current.attempt_no = res.attempt_no;
    current.history = [...(current.history || []), { attempt_no: res.attempt_no, score: res.score }];
    ui.attempts.textContent =
      "历次：" + current.history.map((h) => `#${h.attempt_no} ${h.score}分`).join(" · ");
    ui.qScore.textContent = `当前 ${res.score} 分（第 ${res.attempt_no} 次）`;
    ui.qScore.className = "score " + (res.score < 60 ? "low" : "high");
    renderReview(current, res);
    if (res.review.rule_reason === "llm_unauthorized") {
      const line = document.createElement("p");
      line.className = "raw";
      line.textContent = FAULT_TEXT.llm_unauthorized;
      ui.advice.parentNode.appendChild(line);
    }
    ui.review.scrollIntoView({ block: "nearest" });
    const stats = await Promise.all([refreshStats(), loadPlan()]);
    renderEmpty(stats[0]);
  } catch (err) {
    // 没写进库就不许走：保留原话，只报失败
    const d = describe(err);
    say(`这次没存进去，别走：${d.title}`, d);
    ui.writeStatus.className = "writestatus bad";
    ui.writeStatus.textContent = "未入库";
  } finally {
    ui.submit.textContent = "交卷";
    setBusy("");
  }
});

ui.box.addEventListener("input", () => {
  if (!busy) ui.submit.disabled = !ui.box.value.trim();
});

// ---------- 下一题 / 再答一次 ----------

ui.next.addEventListener("click", async () => {
  if (busy || !current) return;
  setBusy("找下一题：同族优先，其次低分…");
  ui.next.textContent = "找下一题…";
  say("");
  try {
    const data = await get(`/api/next?after=${current.id}`);
    if (!data.question) {
      say("相似题和题单都空了，先用 init.py 导题库");
      return;
    }
    await loadQuestion(data.question.id, `下一题：${data.reason}`);
  } catch (err) {
    fail(err, "换题失败：");
  } finally {
    ui.next.textContent = "下一题";
    setBusy("");
  }
});

ui.retry.addEventListener("click", () => {
  if (!current) return;
  ui.review.hidden = true;
  ui.revNote.hidden = true;
  ui.form.hidden = false;
  ui.box.value = "";
  ui.submit.disabled = true;
  ui.writeStatus.textContent = "";
  ui.writeStatus.className = "writestatus";
  say("");
  ui.box.focus();
});

// ---------- 错题本抽屉 ----------

async function openDrawer() {
  ui.drawer.hidden = false;
  ui.wrongBtn.setAttribute("aria-expanded", "true");
  ui.wrongList.replaceChildren(noneItem("读取中…"));
  try {
    const data = await get("/api/wrongbook");
    ui.wrongMax.textContent = data.max_score;
    ui.wrongList.replaceChildren();
    if (!data.rows.length) {
      ui.wrongList.appendChild(noneItem("目前没有低于 60 分的题"));
      return;
    }
    for (const row of data.rows) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "wrongItem";
      btn.replaceChildren(tagSpan(row.current_score, "s"), tagSpan(`题 ${row.id} · ${row.stem}`));
      btn.addEventListener("click", () => {
        closeDrawer();
        showQuestion(row.id);
      });
      const item = document.createElement("li");
      item.appendChild(btn);
      ui.wrongList.appendChild(item);
    }
  } catch (err) {
    ui.wrongList.replaceChildren(noneItem(`读取失败：${describe(err).title}`));
  }
}

function closeDrawer() {
  ui.drawer.hidden = true;
  ui.wrongBtn.setAttribute("aria-expanded", "false");
}

ui.wrongBtn.addEventListener("click", () => (ui.drawer.hidden ? openDrawer() : closeDrawer()));
ui.drawerClose.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !ui.drawer.hidden) closeDrawer();
});

async function startToday() {
  if (busy) return;
  setBusy("出今天的题…");
  try {
    const data = await loadPlan();
    if (data && data.plan.length) await loadQuestion(data.plan[0].id);
    else say("题单是空的，先跑 python db/init.py 导入题库");
  } finally {
    setBusy("");
  }
}

ui.start.addEventListener("click", startToday);
ui.emptyStart.addEventListener("click", startToday);

// ---------- 首屏 ----------

(async function boot() {
  setBusy("连接 seekdb…");
  try {
    const [stats] = await Promise.all([refreshStats(), loadPlan()]);
    ui.lastStats = stats;
    renderEmpty(stats);
  } finally {
    setBusy("");
  }
  const q = new URLSearchParams(location.search).get("q");
  if (q) await showQuestion(Number(q));   // 直达某道题，错题本跳转和录屏都用这个
})();
