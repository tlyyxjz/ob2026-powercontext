/**
 * 量化前后对照：同一批文档、同一组查询，用 FP32 与 INT8 各算一遍向量，
 * 比较「检索质量有没有掉」和「体积 / 速度换来了多少」。
 *
 * ⛔ 这是**离线评测脚本**，不是应用运行时路径。
 *    应用里向量只可能在浏览器 Worker 里算（server/seekdb.mjs 根本不提供推理接口）；
 *    这里跑在 Node 里，只是为了能在同一进程内把两套权重放在一起比 —— 浏览器里
 *    同时挂两个 pipeline 会互相挤占内存，且无法保证两次测量环境一致。
 *
 * 用法：node scripts/bench.mjs
 * 依赖：models/all-MiniLM-L6-v2/ 下的 model.onnx（FP32）与 model_quantized.onnx（INT8）
 */
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { pipeline, env } from "@huggingface/transformers";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const MODEL_DIR = path.join(ROOT, "models", "all-MiniLM-L6-v2");

env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = path.join(ROOT, "models") + path.sep;

const MODEL = "all-MiniLM-L6-v2";
const TOP_K = 5;

/** 查询 → 人工判定的相关文档（只按语义相关性标注，与关键字是否重合无关）。 */
const CASES = [
  { q: "HNSW 的 ef_construction 参数有什么作用", rel: ["s03", "s16"] },
  { q: "怎么提高搜索的召回率", rel: ["s14", "s09", "s03"] },
  { q: "写入数据之后查询不到新内容", rel: ["s11"] },
  { q: "把模型压到更小的体积", rel: ["s05", "s24"] },
  { q: "浏览器里跑推理怎么不卡住界面", rel: ["s07", "s06"] },
  { q: "中文为什么要专门的分词器", rel: ["s10", "s01", "s22"] },
  { q: "向量检索为什么不是百分之百准确", rel: ["s04"] },
  { q: "不想把文档上传到云端", rel: ["s13"] },
  { q: "第一次查询特别慢的原因", rel: ["s18"] },
  { q: "两路检索结果怎么合并排序", rel: ["s02", "s23"] },
  { q: "批量推理为什么比逐条快", rel: ["s20"] },
  { q: "显卡不可用时怎么办", rel: ["s15"] },
];

const cosine = (a, b) => {
  let dot = 0;
  for (let i = 0; i < a.length; i += 1) dot += a[i] * b[i];
  return dot;
};

async function embedAll(pipe, texts) {
  const tensor = await pipe(texts, { pooling: "mean", normalize: true });
  const dim = tensor.dims[tensor.dims.length - 1];
  const out = [];
  for (let r = 0; r < texts.length; r += 1) {
    out.push(Array.from(tensor.data.slice(r * dim, (r + 1) * dim)));
  }
  return out;
}

async function measure(pipe, docs, label) {
  const t0 = performance.now();
  const docVectors = await embedAll(pipe, docs.map((d) => d.text));
  const docMs = +(performance.now() - t0).toFixed(1);

  // 单条查询延迟：逐条算，取中位数，避免被第一条的算子预热拉偏。
  const single = [];
  for (let round = 0; round < 3; round += 1) {
    for (const c of CASES) {
      const s = performance.now();
      await embedAll(pipe, [c.q]);
      single.push(performance.now() - s);
    }
  }
  single.sort((a, b) => a - b);
  const singleMs = +single[Math.floor(single.length / 2)].toFixed(1);

  const perCase = [];
  for (const c of CASES) {
    const [qv] = await embedAll(pipe, [c.q]);
    const scored = docs
      .map((d, i) => ({ id: d.id, score: cosine(qv, docVectors[i]) }))
      .sort((a, b) => b.score - a.score);
    const top = scored.slice(0, TOP_K).map((x) => x.id);

    const hit = top.filter((id) => c.rel.includes(id));
    const denom = Math.min(c.rel.length, TOP_K);
    const recall = hit.length / denom;
    const mrr = 1 / (top.findIndex((id) => c.rel.includes(id)) + 1) || 0;

    perCase.push({
      q: c.q,
      rel: c.rel,
      top,
      recall: +recall.toFixed(3),
      mrr: mrr === Infinity ? 0 : +mrr.toFixed(3),
    });
  }

  const recall = perCase.reduce((s, c) => s + c.recall, 0) / perCase.length;
  const mrr = perCase.reduce((s, c) => s + c.mrr, 0) / perCase.length;

  return {
    label,
    docMs,
    singleMs,
    recallAt5: +recall.toFixed(4),
    mrrAt5: +mrr.toFixed(4),
    docVectors,
    perCase,
  };
}

function bytesOf(rel) {
  try {
    return fs.statSync(path.join(MODEL_DIR, rel)).size;
  } catch {
    return null;
  }
}

const docs = JSON.parse(await fsp.readFile(path.join(ROOT, "samples", "notes.json"), "utf8"));

const results = {};
for (const [label, dtype] of [
  ["FP32", "fp32"],
  ["INT8", "q8"],
]) {
  process.stdout.write(`\n[${label}] 加载权重 dtype=${dtype} …\n`);
  const t0 = performance.now();
  const pipe = await pipeline("feature-extraction", MODEL, { dtype, device: "cpu" });
  const loadMs = +(performance.now() - t0).toFixed(1);
  process.stdout.write(`[${label}] 加载完成 ${loadMs} ms\n`);
  const r = await measure(pipe, docs, label);
  results[label] = { ...r, loadMs };
}

// ---- 交叉对照：两个模型的排序有多一致 ----
const a = results.FP32.perCase;
const b = results.INT8.perCase;
const overlap = a.map((c, i) => {
  const other = b[i].top;
  const inter = c.top.filter((id) => other.includes(id)).length;
  return inter / Math.max(c.top.length, other.length);
});
const sameTop1 = a.filter((c, i) => c.top[0] === b[i].top[0]).length;

let maxDelta = 0;
let sumDelta = 0;
let n = 0;
for (let i = 0; i < results.FP32.docVectors.length; i += 1) {
  const v = results.FP32.docVectors[i];
  const w = results.INT8.docVectors[i];
  for (let j = 0; j < v.length; j += 1) {
    const d = Math.abs(v[j] - w[j]);
    if (d > maxDelta) maxDelta = d;
    sumDelta += d;
    n += 1;
  }
}

const fp32Bytes = bytesOf("onnx/model.onnx");
const int8Bytes = bytesOf("onnx/model_quantized.onnx");

const report = {
  model: MODEL,
  docs: docs.length,
  cases: CASES.length,
  bytes: { fp32: fp32Bytes, int8: int8Bytes, ratio: +(fp32Bytes / int8Bytes).toFixed(3) },
  quality: {
    fp32: { recallAt5: results.FP32.recallAt5, mrrAt5: results.FP32.mrrAt5 },
    int8: { recallAt5: results.INT8.recallAt5, mrrAt5: results.INT8.mrrAt5 },
  },
  speed: {
    fp32: { loadMs: results.FP32.loadMs, docMs: results.FP32.docMs, singleMs: results.FP32.singleMs },
    int8: { loadMs: results.INT8.loadMs, docMs: results.INT8.docMs, singleMs: results.INT8.singleMs },
  },
  agreement: {
    top5OverlapMean: +(overlap.reduce((s, x) => s + x, 0) / overlap.length).toFixed(4),
    sameTop1: `${sameTop1}/${CASES.length}`,
  },
  vectorDelta: { max: +maxDelta.toFixed(6), mean: +(sumDelta / n).toFixed(6) },
  perCase: { fp32: results.FP32.perCase, int8: results.INT8.perCase },
};

await fsp.writeFile(path.join(ROOT, "bench-report.json"), JSON.stringify(report, null, 2), "utf8");

const pad = (s, w) => String(s).padEnd(w);
const row = (cells, widths) => cells.map((c, i) => pad(c, widths[i])).join("  ");

process.stdout.write(
  [
    "",
    "================ 量化前后对照 ================",
    row(["指标", "FP32", "INT8", "变化"], [22, 14, 14, 12]),
    row(["-".repeat(22), "-".repeat(14), "-".repeat(14), "-".repeat(12)], [22, 14, 14, 12]),
    row(["权重体积", `${fp32Bytes} B`, `${int8Bytes} B`, `${report.bytes.ratio}× 更小`], [22, 14, 14, 12]),
    row(["Recall@5", report.quality.fp32.recallAt5, report.quality.int8.recallAt5, delta(report.quality.fp32.recallAt5, report.quality.int8.recallAt5)], [22, 14, 14, 12]),
    row(["MRR@5", report.quality.fp32.mrrAt5, report.quality.int8.mrrAt5, delta(report.quality.fp32.mrrAt5, report.quality.int8.mrrAt5)], [22, 14, 14, 12]),
    row(["模型加载", `${report.speed.fp32.loadMs} ms`, `${report.speed.int8.loadMs} ms`, ""], [22, 14, 14, 12]),
    row([`${docs.length} 条批量`, `${report.speed.fp32.docMs} ms`, `${report.speed.int8.docMs} ms`, ""], [22, 14, 14, 12]),
    row(["单条中位延迟", `${report.speed.fp32.singleMs} ms`, `${report.speed.int8.singleMs} ms`, ""], [22, 14, 14, 12]),
    row(["Top5 排序重合度", "", report.agreement.top5OverlapMean, ""], [22, 14, 14, 12]),
    row(["Top1 一致", "", report.agreement.sameTop1, ""], [22, 14, 14, 12]),
    row(["向量分量最大偏差", "", report.vectorDelta.max, ""], [22, 14, 14, 12]),
    "",
    `明细已写入 ${path.join(ROOT, "bench-report.json")}`,
    "",
  ].join("\n")
);

function delta(x, y) {
  const d = y - x;
  if (Math.abs(d) < 0.00005) return "持平";
  return `${d > 0 ? "+" : ""}${d.toFixed(4)}`;
}
