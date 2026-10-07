import { declarations, digest } from './repository.mjs';
import { existsSync } from 'node:fs';

export function searchDocuments(files, catalog) {
  return files.flatMap(file => {
    const capabilities = catalog.entries.filter(entry => entry.path === file.path);
    const symbols = declarations(file.path, file.text).map(x => x.symbol);
    const overview = capabilities.map(entry => `${entry.purpose}. ${entry.boundaries}. ${entry.symbols.join(' ')}`).join('\n');
    const text = `${overview}\n${file.path}\n${symbols.join(' ').slice(0, 1000)}\n${file.text.slice(0, 600)}`;
    return [{ id: file.path, path: file.path, line: 1, symbol: '(file summary)', text, sourceHash: digest(file.text) },
      ...capabilities.map(entry => ({ id: `capability:${entry.id}`, path: file.path,
        line: declarations(file.path, file.text).find(item => item.symbol === entry.symbols[0])?.line || 1,
        symbol: entry.symbols[0], text: entry.purpose, sourceHash: digest(file.text),
        boundaries: entry.boundaries,
      })),
    ];
  });
}

function validVector(vector) {
  return Array.isArray(vector) && vector.length > 0 && vector.every(Number.isFinite)
    && vector.some(value => value !== 0);
}

export function rank(rows, query, limit = 10) {
  if (!validVector(query)) throw new Error('Invalid query embedding');
  const norm = vector => Math.sqrt(vector.reduce((sum, value) => sum + value * value, 0));
  return rows.map(row => {
    if (!validVector(row.vector) || row.vector.length !== query.length) throw new Error('Invalid embedding dimension; rebuild index');
    const score = row.vector.reduce((sum, value, index) => sum + value * query[index], 0) / (norm(row.vector) * norm(query));
    const { vector: _vector, ...result } = row;
    return { ...result, score };
  }).sort((a, b) => b.score - a.score).slice(0, limit);
}

export async function updateVectors(documents, cache, modelKey, embed, progress = () => {}) {
  const old = new Map(cache?.version === 1 && cache.modelKey === modelKey
    ? cache.rows.map(row => [row.id, row]) : []);
  const rows = documents.map(doc => {
    const hash = digest(doc.text);
    const previous = old.get(doc.id);
    return { ...doc, hash, vector: previous?.hash === hash && validVector(previous.vector) ? previous.vector : null };
  });
  const missing = rows.filter(row => !row.vector);
  for (let i = 0; i < missing.length; i += 8) {
    const batch = missing.slice(i, i + 8);
    const vectors = await embed(batch.map(row => row.text));
    if (!Array.isArray(vectors) || vectors.length !== batch.length || vectors.some(vector => !validVector(vector))) throw new Error('Invalid embedding output');
    batch.forEach((row, index) => { row.vector = vectors[index]; });
    progress(Math.min(i + 8, missing.length), missing.length);
  }
  return { version: 1, modelKey, rows };
}

export async function localEmbedder(root, { offline = false } = {}) {
  const { AutoTokenizer, AutoModel, mean_pooling, env } = await import('@huggingface/transformers');
  const { resolve } = await import('node:path');
  const { EnvHttpProxyAgent, getGlobalDispatcher, setGlobalDispatcher } = await import('undici');
  const originalDispatcher = getGlobalDispatcher();
  const proxy = !offline && (process.env.HTTPS_PROXY || process.env.https_proxy || process.env.HTTP_PROXY || process.env.http_proxy)
    ? new EnvHttpProxyAgent() : null;
  if (proxy) setGlobalDispatcher(proxy);
  const closeProxy = async () => { if (proxy) { setGlobalDispatcher(originalDispatcher); await proxy.close(); } };
  env.cacheDir = resolve(root, '.cache/reuse/models');
  env.allowRemoteModels = !offline;
  const model = 'Xenova/paraphrase-multilingual-MiniLM-L12-v2';
  const revision = '2c4055b12046f11709e9df2c122e59ffbdc2f900';
  const modelKey = `${model}@${revision}:q8:mean:normalized:256:v1`;
  let tokenizer, encoder;
  try {
    // Load explicit components: pipeline auto-discovery in Transformers.js 4
    // probes unversioned metadata even when a revision is supplied, breaking offline use.
    const options = { revision, local_files_only: offline };
    const tokenizerDirectory = resolve(env.cacheDir, model, revision);
    const cachedTokenizer = existsSync(resolve(tokenizerDirectory, 'tokenizer.json'))
      && existsSync(resolve(tokenizerDirectory, 'tokenizer_config.json'));
    tokenizer = await AutoTokenizer.from_pretrained(cachedTokenizer ? tokenizerDirectory : model, options);
    encoder = await AutoModel.from_pretrained(model, {
      ...options, dtype: 'q8', device: 'cpu',
      session_options: { intraOpNumThreads: 2, interOpNumThreads: 1 },
    });
  } catch (error) {
    await closeProxy();
    throw new Error(`Local embedding model unavailable (${error.cause?.code || error.message}). Check network/proxy or download once before --offline; --lexical is an explicit fallback.`, { cause: error });
  }
  return {
    modelKey,
    embed: async texts => {
      const input = tokenizer(texts, { padding: true, truncation: true, max_length: 256 });
      const output = await encoder(input);
      return mean_pooling(output.last_hidden_state, input.attention_mask).normalize(2, -1).tolist();
    },
    dispose: async () => { try { await encoder.dispose(); } finally { await closeProxy(); } },
  };
}

export function rankFiles(rows, vector, limit = 6) {
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error('--file-limit must be an integer from 1 to 100');
  const seen = new Set();
  return rank(rows, vector, rows.length).filter(row => {
    if (seen.has(row.path)) return false;
    seen.add(row.path);
    return true;
  }).slice(0, limit);
}
