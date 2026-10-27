/**
 * seekdb 数据层封装。
 *
 * 这里只做三件事：建集合、写入并刷新索引、调用数据库原生的混合检索。
 * ⛔ 刻意不提供「分别取向量结果和全文结果、再在 Node 侧拼合」的接口：
 *    题面要求融合必须由 seekdb 完成，Collection.hybridSearch 走的是库内的
 *    DBMS_HYBRID_SEARCH 过程，RRF 分数由数据库算好返回。
 */
import { SeekdbClient } from "seekdb";

/** 实测：hybridSearch 返回的 ids/distances 是「按 query 分组」的嵌套数组。 */
const first = (v) => (Array.isArray(v) && Array.isArray(v[0]) ? v[0] : v);

/** 集合维度固定，必须与浏览器端 embedding 模型的输出维度一致。 */
export const VECTOR_DIMENSION = 384;

/**
 * 把一句自然语言查询摊成 seekdb 全文通道能吃的 whereDocument。
 *
 * 为什么不能直接把 queryText 丢进 $contains：实测 seekdb 的 $contains 是**子串匹配**
 * （等价 LIKE '%x%'），不是分词后的 AND。整句「HNSW 召回率怎么调」作为子串在任何
 * 文档里都不出现，全文通道会恒空 —— 混合检索就退化成纯向量，等于没做混合。
 *
 * 所以这里摊成若干可字面命中的检索项：
 *   - 英文/数字 token：按词边界切，长度 ≥ 2，原样保留（参数名、错误码靠它精确命中）
 *   - 中文：2-gram 滑窗（无词典也能切，且「召回」能命中「召回率」）
 * 再用 $or 组合，语义上等价「任一词条字面出现即算全文命中」。
 */
export function buildWhereDocument(queryText, { maxTerms = 16 } = {}) {
  const text = String(queryText ?? "");
  const terms = [];

  for (const m of text.matchAll(/[A-Za-z][A-Za-z0-9_+\-.]*[A-Za-z0-9]|[A-Za-z]{2,}|\d{2,}/g)) {
    if (!terms.includes(m[0])) terms.push(m[0]);
  }

  // 只在非 ASCII 片段上做 2-gram，避免把英文也切碎。
  for (const chunk of text.match(/[\u4e00-\u9fff]+/g) ?? []) {
    for (let i = 0; i + 2 <= chunk.length; i += 1) {
      const gram = chunk.slice(i, i + 2);
      if (!terms.includes(gram)) terms.push(gram);
    }
  }

  const picked = terms.slice(0, maxTerms);
  if (picked.length === 0) return null;
  if (picked.length === 1) return { $contains: picked[0] };
  return { $or: picked.map((t) => ({ $contains: t })) };
}

/**
 * 首次 refresh_index 在本机实测会撞 34.7s 的内部超时（ret=-4012），
 * 重试一次即成功（403ms）。所以刷新一律走「带退避的重试」，
 * 并把每次尝试的真实耗时回报出去，便于在界面和技术说明里如实呈现。
 */
async function refreshWithRetry(collection, attempts = 3) {
  const tries = [];
  for (let i = 1; i <= attempts; i += 1) {
    const started = performance.now();
    try {
      await collection.refresh_index();
      tries.push({ attempt: i, ok: true, ms: +(performance.now() - started).toFixed(1) });
      return { ok: true, tries };
    } catch (error) {
      tries.push({
        attempt: i,
        ok: false,
        ms: +(performance.now() - started).toFixed(1),
        error: String(error?.message ?? error).slice(0, 200),
      });
      // 失败基本是内部超时，直接重试；保留最后一次的错误向外抛。
      if (i === attempts) return { ok: false, tries, error: tries.at(-1).error };
      await new Promise((r) => setTimeout(r, 200 * i));
    }
  }
  return { ok: false, tries };
}

export async function openStore({ dir, collectionName = "kb" }) {
  const client = new SeekdbClient({ path: dir });
  // embeddingFunction 传 null：向量由浏览器算好再传上来，服务端不做任何推理。
  const collection = await client.getOrCreateCollection({
    name: collectionName,
    embeddingFunction: null,
  });

  return {
    dimension: collection.dimension,
    distance: collection.distance,

    /** 集合里的真实条数。界面入库计数必须用它，不能用前端累加 —— 刷新后累加值会归零。 */
    async count() {
      return collection.count();
    },

    /** 集合当前的 schema，用来说明「向量索引 + 全文索引都是数据库侧的」。 */
    schema() {
      return collection.schema ? collection.schema.toMetadataJson() : null;
    },

    /**
     * 写入文档并刷新索引。vectors 由浏览器端模型产出，这里只负责落库。
     * 返回分阶段耗时，供界面和压测使用。
     */
    async ingest({ ids, texts, vectors, metadatas }) {
      const tAdd = performance.now();
      await collection.add({
        ids,
        embeddings: vectors,
        documents: texts,
        metadatas: metadatas ?? ids.map(() => ({})),
      });
      const addMs = +(performance.now() - tAdd).toFixed(1);

      const refresh = await refreshWithRetry(collection);

      const tCount = performance.now();
      const total = await collection.count();
      const countMs = +(performance.now() - tCount).toFixed(1);

      return {
        added: ids.length,
        total,
        addMs,
        refreshMs: refresh.tries.reduce((sum, t) => sum + t.ms, 0),
        refreshTries: refresh.tries,
        refreshOk: refresh.ok,
        countMs,
      };
    },

    /**
     * 混合检索：一次调用、数据库内融合。
     * queryText 用于全文通道（$contains），queryVector 用于向量通道。
     */
    async hybridSearch({ queryText, queryVector, k = 10, rankConstant = 60 }) {
      const started = performance.now();
      const where = buildWhereDocument(queryText);
      const result = await collection.hybridSearch({
        query: { whereDocument: where ?? { $contains: queryText } },
        knn: { queryEmbeddings: [queryVector] },
        rank: { rrf: { rankWindowSize: 40, rankConstant } },
        nResults: k,
        include: ["documents", "metadatas", "distances"],
      });
      const dbMs = +(performance.now() - started).toFixed(1);

      const ids = first(result.ids) ?? [];
      const scores = first(result.distances) ?? [];
      const documents = first(result.documents) ?? [];
      const metadatas = first(result.metadatas) ?? [];

      return {
        dbMs,
        whereDocument: where,
        hits: ids.map((id, i) => ({
          id,
          // 库内 RRF 分数：单通道命中约为 1/(rankConstant+rank)，双通道约为两倍。
          rrf: scores[i],
          text: documents[i] ?? null,
          metadata: metadatas[i] ?? null,
        })),
      };
    },

    /** 仅向量通道，用于对照（说明混合检索确实比单通道多召回）。 */
    async vectorOnly({ queryVector, k = 10 }) {
      const started = performance.now();
      const result = await collection.query({
        queryEmbeddings: [queryVector],
        nResults: k,
        include: ["documents", "distances"],
      });
      const dbMs = +(performance.now() - started).toFixed(1);
      const ids = first(result.ids) ?? [];
      const distances = first(result.distances) ?? [];
      return {
        dbMs,
        hits: ids.map((id, i) => ({ id, distance: distances[i] })),
      };
    },

    async close() {
      await client.close();
    },
  };
}
