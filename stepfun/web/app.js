/**
 * 前端编排：模型的加载与推理全在 Worker（= 用户设备）里完成，
 * 主线程只负责切块、调接口、渲染读数。
 *
 * 三个硬约束（来自题面）：
 *   1. 向量必须在这里、在浏览器里算出来 —— 服务端只收结果，不做推理。
 *   2. 检索必须调 seekdb 的 hybridSearch —— 不在客户端分别取两路再拼。
 *   3. 后端要明确展示；WebGPU 不可用时要自动降级并说明原因。
 */

const MODEL = "all-MiniLM-L6-v2";
const DTYPE = "q8"; // INT8 量化权重
const RANK_CONSTANT = 60;

const $ = (id) => document.getElementById(id);
const el = {
  badgeDevice: $("badge-device"),
  badgeModel: $("badge-model"),
  badgeDb: $("badge-db"),
  badgeDocs: $("badge-docs"),
  btnLoad: $("btn-load"),
  btnSamples: $("btn-samples"),
  btnPaste: $("btn-paste"),
  paste: $("paste"),
  q: $("q"),
  btnSearch: $("btn-search"),
  prog: $("prog"),
  log: $("log"),
  hits: $("hits"),
  placeholder: $("placeholder"),
  timingBody: $("timing-body"),
  techBody: $("tech-body"),
};

const state = {
  worker: null,
  ready: false,
  device: null,
  fallbackReason: null,
  docs: 0,
  seq: 0,
  pending: new Map(),
  timings: new Map(), // key -> { ms, note }
  lastWhere: null,
};

/* ------------------------------------------------------------------ 工具 */

const nf = new Intl.NumberFormat("en-US");
const num = (n) => nf.format(n);
const ms = (v) => `${v < 10 ? v.toFixed(1) : Math.round(v)} ms`;

function log(msg, kind = "") {
  const row = document.createElement("div");
  const t = document.createElement("time");
  t.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const s = document.createElement("span");
  s.className = kind;
  s.textContent = msg;
  row.append(t, s);
  el.log.append(row);
  el.log.scrollTop = el.log.scrollHeight;
}

function timing(key, ms_, note) {
  state.timings.set(key, { ms: ms_, note });
  renderTimings();
}

const ORDER = [
  ["load", "模型加载（含权重 + 会话初始化）"],
  ["embed-doc", "文档向量化（浏览器内）"],
  ["add", "写入 seekdb"],
  ["refresh", "索引刷新"],
  ["embed-query", "查询向量化（浏览器内）"],
  ["search", "库内混合检索"],
];

function renderTimings() {
  el.timingBody.innerHTML = "";
  let any = false;
  for (const [key, label] of ORDER) {
    const rec = state.timings.get(key);
    if (!rec) continue;
    any = true;
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${label}</td><td>${ms(rec.ms)}</td><td>${rec.note ?? ""}</td>`;
    el.timingBody.append(tr);
  }
  if (!any) el.timingBody.innerHTML = '<tr><td colspan="3" class="empty">尚未运行</td></tr>';
}

function setBadge(node, text, kind) {
  node.className = `badge ${kind}`;
  node.innerHTML = `<i class="dot"></i>${text}`;
}

/* --------------------------------------------------------- Worker 通信 */

function callWorker(type, payload, onProgress) {
  const requestId = ++state.seq;
  return new Promise((resolve, reject) => {
    state.pending.set(requestId, { resolve, reject, onProgress });
    state.worker.postMessage({ type, requestId, ...payload });
  });
}

function initWorker() {
  // type:"module" 是必需的：transformers.min.js 是 ESM，importScripts 会崩（见 worker 顶部注释）。
  const worker = new Worker("/worker-embed.js", { type: "module" });
  state.worker = worker;

  worker.addEventListener("message", (event) => {
    const msg = event.data;

    if (msg.type === "load-progress") {
      const pct = Math.round(msg.progress);
      el.prog.classList.add("on");
      el.prog.firstElementChild.style.width = `${pct}%`;
      // 只写一条，避免刷屏：直接改最后一行的文案。
      const last = el.log.lastElementChild;
      const text = `下载 ${msg.file ?? "权重"} ${pct}%（${msg.device}）`;
      if (last?.dataset.progress === "1") last.lastElementChild.textContent = text;
      else {
        log(text);
        el.log.lastElementChild.dataset.progress = "1";
      }
      return;
    }

    const handler = state.pending.get(msg.requestId);

    if (msg.type === "embed-progress") {
      handler?.onProgress?.(msg);
      return;
    }

    if (!handler) return;

    if (msg.type === "ready") {
      state.pending.delete(msg.requestId);
      el.prog.classList.remove("on");
      handler.resolve(msg);
    } else if (msg.type === "embedded") {
      state.pending.delete(msg.requestId);
      handler.resolve(msg);
    } else if (msg.type === "init-failed") {
      handler.onProgress?.(msg);
    } else if (msg.type === "fatal" || msg.type === "embed-failed") {
      state.pending.delete(msg.requestId);
      handler.reject(new Error(msg.error));
    }
  });

  worker.addEventListener("error", (e) => log(`Worker 错误：${e.message}`, "err"));
}

/* ------------------------------------------------------------ 模型加载 */

async function detectWebgpu() {
  try {
    if (!navigator.gpu) return { ok: false, reason: "navigator.gpu 不存在（浏览器未启用 WebGPU）" };
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) return { ok: false, reason: "requestAdapter() 返回 null（没有可用适配器）" };
    return { ok: true, reason: null, vendor: adapter.info?.vendor ?? "" };
  } catch (error) {
    return { ok: false, reason: String(error?.message ?? error) };
  }
}

async function loadModel() {
  el.btnLoad.disabled = true;
  el.prog.classList.add("on");
  el.prog.firstElementChild.style.width = "0%";

  const gpu = await detectWebgpu();
  log(gpu.ok ? "检测到 WebGPU 适配器，首选 WebGPU 后端" : `WebGPU 不可用：${gpu.reason}`);

  try {
    const res = await callWorker(
      "init",
      { model: MODEL, dtype: DTYPE, preferWebgpu: gpu.ok },
      (m) => log(`WebGPU 初始化失败，降级中：${m.error}`, "err")
    );

    state.ready = true;
    state.device = res.device;
    state.fallbackReason = res.fallbackReason ?? (gpu.ok ? null : gpu.reason);

    const degraded = state.device !== "webgpu";
    setBadge(
      el.badgeDevice,
      `推理后端 <b>${state.device === "webgpu" ? "WebGPU" : "WASM"}</b>${degraded ? " · 已降级" : ""}`,
      state.device === "webgpu" ? "ok" : "warn"
    );
    setBadge(el.badgeModel, `模型 <b>${MODEL}</b> · ${DTYPE === "q8" ? "INT8" : DTYPE.toUpperCase()}`, "ok");
    timing("load", res.loadMs, `${state.device}${res.dim ? ` · 输出 ${res.dim} 维` : ""}`);

    log(`模型就绪：${res.loadMs} ms，后端 ${state.device}`, "ok");
    if (degraded) log(`降级原因：${state.fallbackReason ?? "未提供"}`);

    el.btnSamples.disabled = false;
    el.btnPaste.disabled = false;
    el.q.disabled = false;
    el.btnSearch.disabled = false;
  } catch (error) {
    el.prog.classList.remove("on");
    setBadge(el.badgeDevice, `推理后端 <b>加载失败</b>`, "err");
    log(`模型加载失败：${error.message}`, "err");
    el.btnLoad.disabled = false;
  }
}

/* ------------------------------------------------------------ 文档切块 */

/** 按段落切；单段过长再按句读切，块间保留 1 句重叠。 */
function chunk(text, { max = 300 } = {}) {
  const out = [];
  for (const para of text.split(/\n\s*\n/)) {
    const clean = para.replace(/\s+/g, " ").trim();
    if (!clean) continue;
    if (clean.length <= max) {
      out.push(clean);
      continue;
    }
    const sentences = clean.split(/(?<=[。！？!?；;])/).filter(Boolean);
    let buf = "";
    for (const s of sentences) {
      if ((buf + s).length > max && buf) {
        out.push(buf.trim());
        buf = s;
      } else buf += s;
    }
    if (buf.trim()) out.push(buf.trim());
  }
  return out;
}

/* ------------------------------------------------------------ 导入流程 */

async function ingestDocs(docs) {
  if (docs.length === 0) {
    log("没有可导入的内容", "err");
    return;
  }

  el.btnSamples.disabled = true;
  el.btnPaste.disabled = true;
  el.prog.classList.add("on");
  el.prog.firstElementChild.style.width = "0%";

  try {
    log(`向量化 ${docs.length} 条（${state.device}）…`);

    const embedded = await callWorker(
      "embed",
      { texts: docs.map((d) => d.text), batchSize: 8 },
      (p) => {
        el.prog.firstElementChild.style.width = `${Math.round((p.done / p.total) * 100)}%`;
      }
    );

    timing("embed-doc", embedded.ms, `${docs.length} 条 · ${state.device}`);

    log(`写入 seekdb…`);
    const res = await fetch("/api/ingest", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ docs, vectors: embedded.vectors }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`);

    timing("add", body.addMs, `${body.added} 条`);
    timing(
      "refresh",
      body.refreshMs,
      body.refreshTries.map((t) => `第${t.attempt}次 ${t.ok ? "成功" : "失败"} ${ms(t.ms)}`).join(" · ")
    );

    state.docs = body.total;
    setBadge(el.badgeDocs, `已入库 <b>${state.docs}</b>`, "ok");
    log(`入库完成：+${body.added} 条，库内共 ${body.total} 条；add ${ms(body.addMs)} / refresh ${ms(body.refreshMs)}（${body.refreshTries.length} 次尝试）`, "ok");
    if (!body.refreshOk) log("索引刷新未成功，新数据可能查不到", "err");

    el.placeholder.textContent = "输入查询，看数据库返回的排序结果。";
  } catch (error) {
    log(`导入失败：${error.message}`, "err");
  } finally {
    el.prog.classList.remove("on");
    el.btnSamples.disabled = false;
    el.btnPaste.disabled = false;
  }
}

/* ------------------------------------------------------------ 检索流程 */

function highlight(text, terms) {
  if (!terms?.length) return escapeHtml(text);
  const ranges = [];
  for (const term of terms) {
    let i = text.indexOf(term);
    while (i >= 0) {
      ranges.push([i, i + term.length]);
      i = text.indexOf(term, i + 1);
    }
  }
  if (!ranges.length) return escapeHtml(text);
  ranges.sort((a, b) => a[0] - b[0] || b[1] - a[1]);

  const merged = [];
  for (const r of ranges) {
    const last = merged.at(-1);
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([...r]);
  }

  let out = "";
  let cursor = 0;
  for (const [s, e] of merged) {
    out += escapeHtml(text.slice(cursor, s));
    out += `<mark>${escapeHtml(text.slice(s, e))}</mark>`;
    cursor = e;
  }
  return out + escapeHtml(text.slice(cursor));
}

const escapeHtml = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function runSearch() {
  const text = el.q.value.trim();
  if (!text) return;

  el.btnSearch.disabled = true;
  el.placeholder?.remove();
  el.hits.innerHTML = '<p class="hint">检索中…</p>';

  try {
    const embedded = await callWorker("embed", { texts: [text], batchSize: 1 });
    timing("embed-query", embedded.ms, `${state.device}`);

    const res = await fetch("/api/search", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, vector: embedded.vectors[0], k: 8 }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`);

    const { hybrid } = body;
    timing("search", hybrid.dbMs, `${hybrid.hits.length} 条`);
    state.lastWhere = hybrid.whereDocument;

    const terms = hybrid.whereDocument?.$or?.map((c) => c.$contains) ?? [hybrid.whereDocument?.$contains].filter(Boolean);
    renderHits(hybrid.hits, terms);
    renderTech(text, hybrid);
  } catch (error) {
    el.hits.innerHTML = `<p class="hint" style="color:var(--red)">检索失败：${escapeHtml(error.message)}</p>`;
    log(`检索失败：${error.message}`, "err");
  } finally {
    el.btnSearch.disabled = false;
  }
}

function renderHits(hits, terms) {
  el.hits.innerHTML = "";
  if (!hits.length) {
    el.hits.innerHTML = '<p class="hint">没有命中。换一个查询，或先导入语料。</p>';
    return;
  }

  // RRF 单通道命中 ≈ 1/(k+1)，双通道 ≈ 2/(k+1)，中间没有取值。
  // 所以用 1.5/(k+1) 作阈值反推通道来源 —— 这是从分数反推，不是数据库直接给的字段。
  const unit = 1 / (RANK_CONSTANT + 1);
  const threshold = 1.5 * unit;

  hits.forEach((hit, i) => {
    const dual = typeof hit.rrf === "number" && hit.rrf > threshold;
    const card = document.createElement("article");
    card.className = `hit ${dual ? "dual" : "vec"}`;
    card.innerHTML = `
      <div class="hit-top">
        <span class="hit-rank">${i + 1}</span>
        <span class="hit-score">RRF ${typeof hit.rrf === "number" ? hit.rrf.toFixed(4) : "—"}</span>
        <span class="tag ${dual ? "dual" : "vec"}">${dual ? "双通道命中" : "仅向量通道"}</span>
        <span class="tag vec">${escapeHtml(hit.id)}</span>
        <span class="tag vec">${escapeHtml(hit.metadata?.topic ?? "")}</span>
      </div>
      <div class="hit-body">${highlight(hit.text ?? "", terms)}</div>`;
    el.hits.append(card);
  });
}

function renderTech(query, hybrid) {
  const unit = 1 / (RANK_CONSTANT + 1);
  const lines = [
    `查询: ${query}`,
    `RRF 单位分 1/(k+1) = ${unit.toFixed(5)}（k=${RANK_CONSTANT}）`,
    `双通道阈值 1.5/(k+1) = ${(1.5 * unit).toFixed(5)}`,
    "",
    "全文通道 whereDocument（由查询自动摊成可字面命中的词条）:",
    JSON.stringify(hybrid.whereDocument, null, 2),
    "",
    "数据库直接返回的 RRF 分数:",
    ...hybrid.hits.map((h, i) => `  #${i + 1} ${h.id}  ${typeof h.rrf === "number" ? h.rrf.toFixed(6) : "—"}  →  ≈ ${typeof h.rrf === "number" ? (h.rrf / unit).toFixed(2) : "—"} × 1/(k+1)`),
    "",
    `库内检索耗时: ${hybrid.dbMs} ms`,
  ];
  el.techBody.textContent = lines.join("\n");
}

/* ---------------------------------------------------------------- 事件 */

el.btnLoad.addEventListener("click", loadModel);

el.btnSamples.addEventListener("click", async () => {
  const res = await fetch("/samples/notes.json");
  if (!res.ok) {
    log(`读取语料失败：HTTP ${res.status}`, "err");
    return;
  }
  const docs = await res.json();
  await ingestDocs(docs);
});

el.btnPaste.addEventListener("click", async () => {
  const raw = el.paste.value.trim();
  if (!raw) {
    log("文本框是空的", "err");
    return;
  }
  const parts = chunk(raw);
  const stamp = Date.now();
  const docs = parts.map((text, i) => ({
    id: `u-${stamp}-${i}`,
    text,
    metadata: { topic: "pasted", source: "user" },
  }));
  log(`切成 ${docs.length} 块`);
  await ingestDocs(docs);
});

el.btnSearch.addEventListener("click", runSearch);
el.q.addEventListener("keydown", (e) => {
  if (e.key === "Enter") runSearch();
});

/* ------------------------------------------------------------ 后端矩阵 */

/**
 * 把离线实测数据摊在界面上。
 * ⛔ 这是**本机实测**（AMD 集显 + 有头 Chromium），不是理论值，
 *    也刻意不写成「WebGPU 一定慢」—— 见数据里的「不外推声明」。
 * 默认策略仍按题面：支持 WebGPU 就优先用 WebGPU。
 */
async function renderBackendMatrix() {
  const box = $("backend-body");
  try {
    const data = await (await fetch("/backend-matrix.json")).json();
    const rows = data.rows ?? [];
    const minVal = Math.min(...rows.map((r) => r["折合单条"]));

    const head = ["场景", "实际后端", "首次", "稳态中位", "折合单条", "模型加载"];
    const body = rows
      .map((r) => {
        const best = r["折合单条"] === minVal ? ' class="best"' : "";
        const gpu = r["后端"] === "webgpu" ? ' class="gpu"' : "";
        return `<tr>
          <td>${escapeHtml(r["场景"])}</td>
          <td${gpu}>${escapeHtml(r["后端"])}</td>
          <td>${r["首次"]} ms</td>
          <td>${r["稳态中位"]} ms</td>
          <td${best}>${r["折合单条"]} ms</td>
          <td>${r["加载"]} ms</td>
        </tr>`;
      })
      .join("");

    box.innerHTML = `
      <table>
        <thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
        <tbody>${body}</tbody>
      </table>
      <div class="notes">
        <ol>${(data["结论"] ?? []).map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ol>
        <p style="margin:10px 0 0;color:var(--dim)">${escapeHtml(data["环境"]?.["无头模式"] ?? "")}　${escapeHtml(
      data["不外推声明"] ?? ""
    )}</p>
      </div>`;
  } catch (error) {
    box.textContent = `读取实测数据失败：${error.message}`;
  }
}

/* ---------------------------------------------------------------- 启动 */

initWorker();
renderTimings();
renderBackendMatrix();

fetch("/api/status")
  .then((r) => r.json())
  .then((s) => {
    setBadge(el.badgeDb, `seekdb <b>${s.dimension} 维 · ${s.distance}</b>`, "ok");
    log(`seekdb 就绪：${s.dimension} 维 / ${s.distance}`);
    // 条数必须问数据库，不能用前端累加：刷新页面后累加值会归零，而库里的数据还在。
    state.docs = s.count ?? 0;
    setBadge(el.badgeDocs, `已入库 <b>${state.docs}</b>`, state.docs > 0 ? "ok" : "");
    if (state.docs > 0) {
      log(`库内已有 ${state.docs} 条，可直接检索`);
      el.placeholder.textContent = "输入查询，看数据库返回的排序结果。";
    }
  })
  .catch((e) => {
    setBadge(el.badgeDb, `seekdb <b>不可用</b>`, "err");
    log(`seekdb 状态读取失败：${e.message}`, "err");
  });
