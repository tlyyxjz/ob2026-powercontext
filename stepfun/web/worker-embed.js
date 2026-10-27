/**
 * 端侧 embedding 推理 Worker。
 *
 * 为什么放在 Worker 里：模型首载 + 推理会占满主线程，放主线程会把界面卡死，
 * 也拿不到「向量化进度」这种分阶段读数。
 *
 * 推理后端：优先 WebGPU，不可用时自动降级到 WASM，并把实际使用的后端报给主线程，
 * 由界面明确展示（题面 2.2 / 3.1 都要求「明确提示当前后端」）。
 *
 * ⚠️ 这里必须是 **module worker**（new Worker(url, {type:"module"})）：
 *    @huggingface/transformers 的 dist/transformers.min.js 是 ESM 构建，
 *    内部用 import.meta.url 推导 wasm 路径。用 importScripts 加载会直接报
 *    `Cannot use 'import.meta' outside a module`，worker 顶层就崩，连 message
 *    都收不到（主线程只能看到「永远 pending」）。
 */
import { pipeline, env } from "/vendor/transformers.min.js";

// ⛔ 只允许从本服务器加载模型：allowRemoteModels=false 之后，
//    任何一个字节的模型文件都不会去 HuggingFace 拉，全部来自 /models/。
env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = "/models/";
// 与 transformers.min.js 同目录存放的 ort wasm，显式指过去，避免相对路径解析到别处。
env.backends.onnx.wasm.wasmPaths = "/vendor/";
env.backends.onnx.wasm.numThreads = 1;

let extractor = null;
let activeDevice = null;

const firstWhere = (values, ok) => values.find(ok);

async function tryInit(device, model, dtype = "q8") {
  const started = performance.now();
  const pipe = await pipeline("feature-extraction", model, {
    device,
    // q8 = INT8 量化权重（对应 onnx/model_quantized.onnx）；
    // fp32 = 原始权重（onnx/model.onnx），只在 bench 里用来做量化前后对照。
    dtype,
    progress_callback: (p) => {
      if (p.status === "progress" && typeof p.progress === "number") {
        self.postMessage({ type: "load-progress", device, file: p.file, progress: p.progress });
      }
    },
  });
  return { pipe, ms: +(performance.now() - started).toFixed(1) };
}

async function embed(texts, batchSize, requestId) {
  const out = [];
  const started = performance.now();
  for (let i = 0; i < texts.length; i += batchSize) {
    const chunk = texts.slice(i, i + batchSize);
    const tensor = await extractor(chunk, { pooling: "mean", normalize: true });
    // tensor.data 是扁平 Float32Array，按「行 = 一条文本」切回来。
    const dim = tensor.dims[tensor.dims.length - 1];
    for (let r = 0; r < chunk.length; r += 1) {
      out.push(Array.from(tensor.data.slice(r * dim, (r + 1) * dim)));
    }
    self.postMessage({
      type: "embed-progress",
      requestId,
      done: Math.min(i + batchSize, texts.length),
      total: texts.length,
    });
  }
  return { vectors: out, ms: +(performance.now() - started).toFixed(1) };
}

self.addEventListener("message", async (event) => {
  const msg = event.data;

  if (msg.type === "init") {
    const order = msg.preferWebgpu ? ["webgpu", "wasm"] : ["wasm"];
    let lastError = null;
    for (const device of order) {
      try {
        const { pipe, ms } = await tryInit(device, msg.model, msg.dtype);
        extractor = pipe;
        activeDevice = device;
        // 记录降级原因：如果首选 webgpu 没成功，界面要能说出为什么。
        self.postMessage({
          type: "ready",
          requestId: msg.requestId,
          device,
          dtype: msg.dtype ?? "q8",
          fallbackReason: device === "webgpu" ? null : lastError,
          loadMs: ms,
          dim: pipe.model?.config?.hidden_size ?? null,
        });
        return;
      } catch (error) {
        lastError = String(error?.message ?? error);
        self.postMessage({ type: "init-failed", requestId: msg.requestId, device, error: lastError });
      }
    }
    self.postMessage({ type: "fatal", requestId: msg.requestId, error: lastError ?? "no backend" });
    return;
  }

  if (msg.type === "embed") {
    if (!extractor) {
      self.postMessage({ type: "fatal", error: "embedding pipeline not initialised" });
      return;
    }
    try {
      const { vectors, ms } = await embed(msg.texts, msg.batchSize ?? 8, msg.requestId);
      self.postMessage({ type: "embedded", requestId: msg.requestId, vectors, ms, device: activeDevice });
    } catch (error) {
      self.postMessage({ type: "embed-failed", requestId: msg.requestId, error: String(error?.message ?? error) });
    }
  }
});
