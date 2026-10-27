/**
 * 本地服务：静态文件 + 两个 API。
 *
 * 分工（题面要求「说明指定技术各自承担什么」）：
 *   - 浏览器：模型加载、文档向量化、查询向量化（全部在用户设备上算）
 *   - 本服务：只做三件事——把浏览器算好的向量写进 seekdb、刷新索引、调用 seekdb 的混合检索
 *   - seekdb：向量与全文的存储、检索与 RRF 融合（融合不在这层做）
 */
import http from "node:http";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { openStore, VECTOR_DIMENSION } from "./seekdb.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const WEB_DIR = path.join(ROOT, "web");
const MODEL_DIR = path.join(ROOT, "models");
const SAMPLES_DIR = path.join(ROOT, "samples");
const VENDOR_DIR = path.join(ROOT, "node_modules", "@huggingface", "transformers", "dist");
const DATA_DIR = process.env.SEEKDB_DATA_DIR ?? path.join(ROOT, "data", "seekdb");

const PORT = Number(process.env.PORT ?? 5173);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".wasm": "application/wasm",
  ".onnx": "application/octet-stream",
  ".map": "application/json; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
};

/** 只允许读这三个目录，避免路径穿越。 */
function resolveStatic(urlPath) {
  const clean = decodeURIComponent(urlPath.split("?")[0]);
  const routes = [
    ["/vendor/", VENDOR_DIR],
    ["/models/", MODEL_DIR],
    ["/samples/", SAMPLES_DIR],
    ["/", WEB_DIR],
  ];
  for (const [prefix, base] of routes) {
    if (prefix === "/" || clean.startsWith(prefix)) {
      const rel = prefix === "/" ? clean : clean.slice(prefix.length);
      const abs = path.resolve(base, "." + path.posix.normalize("/" + rel));
      if (!abs.startsWith(base)) return null;
      return abs;
    }
  }
  return null;
}

const json = (res, code, body) => {
  const payload = JSON.stringify(body);
  res.writeHead(code, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    "cache-control": "no-store",
  });
  res.end(payload);
};

async function readBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 64 * 1024 * 1024) throw new Error("request body too large");
    chunks.push(chunk);
  }
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
}

const store = await openStore({ dir: DATA_DIR });

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");

  try {
    if (url.pathname === "/api/status") {
      json(res, 200, {
        dimension: store.dimension,
        expectedDimension: VECTOR_DIMENSION,
        distance: store.distance,
        count: await store.count(),
        schema: store.schema(),
        dataDir: DATA_DIR,
      });
      return;
    }

    if (url.pathname === "/api/ingest" && req.method === "POST") {
      const body = await readBody(req);
      const docs = body.docs ?? [];
      const vectors = body.vectors ?? [];
      if (docs.length !== vectors.length) {
        json(res, 400, { error: `docs(${docs.length}) 与 vectors(${vectors.length}) 数量不一致` });
        return;
      }
      for (const v of vectors) {
        if (!Array.isArray(v) || v.length !== store.dimension) {
          json(res, 400, { error: `向量维度必须是 ${store.dimension}，收到 ${Array.isArray(v) ? v.length : typeof v}` });
          return;
        }
      }
      const result = await store.ingest({
        ids: docs.map((d) => d.id),
        texts: docs.map((d) => d.text),
        vectors,
        metadatas: docs.map((d) => d.metadata ?? {}),
      });
      json(res, 200, result);
      return;
    }

    if (url.pathname === "/api/search" && req.method === "POST") {
      const body = await readBody(req);
      if (!body.text || !Array.isArray(body.vector)) {
        json(res, 400, { error: "需要 text 和 vector" });
        return;
      }
      if (body.vector.length !== store.dimension) {
        json(res, 400, { error: `向量维度必须是 ${store.dimension}` });
        return;
      }
      const hybrid = await store.hybridSearch({ queryText: body.text, queryVector: body.vector, k: body.k ?? 10 });
      // 对照组：仅向量通道。前端拿它来说明混合检索多召回了什么。
      const vectorOnly = body.withVectorOnly ? await store.vectorOnly({ queryVector: body.vector, k: body.k ?? 10 }) : null;
      json(res, 200, { hybrid, vectorOnly });
      return;
    }

    // ---- 静态 ----
    let filePath = resolveStatic(url.pathname);
    if (!filePath) {
      json(res, 403, { error: "forbidden" });
      return;
    }
    if (url.pathname === "/") filePath = path.join(WEB_DIR, "index.html");
    let stat = await fsp.stat(filePath).catch(() => null);
    if (stat?.isDirectory()) {
      filePath = path.join(filePath, "index.html");
      stat = await fsp.stat(filePath).catch(() => null);
    }
    if (!stat) {
      json(res, 404, { error: "not found", path: url.pathname });
      return;
    }
    res.writeHead(200, {
      "content-type": MIME[path.extname(filePath).toLowerCase()] ?? "application/octet-stream",
      "content-length": stat.size,
      // 模型和 wasm 允许缓存，避免每次刷新重下 23 MB。
      "cache-control": url.pathname.startsWith("/models/") ? "public, max-age=86400" : "no-cache",
    });
    fs.createReadStream(filePath).pipe(res);
  } catch (error) {
    json(res, 500, { error: String(error?.message ?? error) });
  }
});

server.listen(PORT, () => {
  process.stdout.write(
    [
      `seekdb 边缘知识库已启动: http://localhost:${PORT}`,
      `  数据目录 : ${DATA_DIR}`,
      `  集合维度 : ${store.dimension}  距离: ${store.distance}`,
      `  模型目录 : ${MODEL_DIR}`,
      "",
    ].join("\n")
  );
});
