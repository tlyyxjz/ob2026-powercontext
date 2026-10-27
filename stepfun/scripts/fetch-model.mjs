/**
 * 拉取端侧 embedding 模型到本地 models/ 目录。
 *
 * 为什么要这一步：题面要求「实际计算必须发生在用户设备」，但也允许「模型文件下载或本地加载」。
 * 本脚本把模型落到本地、由本服务以静态文件提供，之后浏览器只从 localhost 取模型，
 * 运行期不会再访问任何远端推理服务。
 *
 * 默认源是 ModelScope 上的 Xenova/all-MiniLM-L6-v2 —— 与 HuggingFace 上是同一份
 * Transformers.js 布局（config/tokenizer/onnx），只是换成国内可达的镜像。
 * 用 --source 可以换成自建镜像或本地目录。
 *
 * 同时下 fp32 与 INT8 两份权重，用来实测量化前后的体积与检索质量（见 scripts/bench.mjs）。
 */
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// 落到 models/<模型名>/ ：worker 里 env.localModelPath="/models/" + model="all-MiniLM-L6-v2"
// 会拼成 /models/all-MiniLM-L6-v2/config.json，路径必须对上这一层。
const MODEL_NAME = process.env.MODEL_NAME ?? "all-MiniLM-L6-v2";
const OUT = path.join(ROOT, "models", MODEL_NAME);

const MIRROR =
  process.env.MODEL_MIRROR ?? "https://www.modelscope.cn/api/v1/models/Xenova/all-MiniLM-L6-v2/repo";

const FILES = [
  "config.json",
  "tokenizer.json",
  "tokenizer_config.json",
  "onnx/model.onnx", // FP32
  "onnx/model_quantized.onnx", // INT8
];

async function download(file) {
  const url = `${MIRROR}?Revision=master&FilePath=${encodeURIComponent(file)}`;
  const started = Date.now();
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${file}: HTTP ${res.status}`);
  const buffer = Buffer.from(await res.arrayBuffer());
  const target = path.join(OUT, file);
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.writeFile(target, buffer);
  return { file, bytes: buffer.length, ms: Date.now() - started };
}

const results = [];
for (const file of FILES) {
  const r = await download(file);
  results.push(r);
  process.stdout.write(`  ${r.file.padEnd(30)} ${String(r.bytes).padStart(12)} B  ${r.ms}ms\n`);
}

const fp32 = results.find((r) => r.file.endsWith("model.onnx"))?.bytes ?? 0;
const int8 = results.find((r) => r.file.endsWith("model_quantized.onnx"))?.bytes ?? 0;

process.stdout.write(
  [
    "",
    `模型目录: ${OUT}`,
    `FP32 权重: ${fp32.toLocaleString()} B`,
    `INT8 权重: ${int8.toLocaleString()} B`,
    `压缩比  : ${(fp32 / int8).toFixed(3)}×`,
    "",
  ].join("\n")
);
