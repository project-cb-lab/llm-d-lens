import assert from 'node:assert/strict';
import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import { localEmbedder, rank } from './search.mjs';

// Explicit opt-in integration test: download via reuse:index first; never runs in CI.
test('real local model retrieves English capabilities from English requirements offline', async () => {
  const model = await localEmbedder(process.cwd(), { offline: true });
  try {
    const docs = [
      { path: 'json', text: 'Read and write JSON files with atomic replacement to avoid partial writes.' },
      { path: 'ui', text: 'Display a loading spinner, an error with retry, or an empty state in a user interface.' },
      { path: 'audio', text: 'Transcribe microphone audio into text using speech recognition.' },
    ];
    const vectors = await model.embed(docs.map(row => row.text));
    const rows = docs.map((doc, i) => ({ ...doc, vector: vectors[i] }));
    for (const [query, expected] of [
      ['Save JSON files atomically to prevent corruption from partial writes', 'json'],
      ['Page loading, retry after request failure, and empty-state messages', 'ui'],
    ]) {
      const [vector] = await model.embed([query]);
      assert.equal(rank(rows, vector, 1)[0].path, expected);
    }
  } finally { await model.dispose(); }
});

test('repository search finds the shared JSON HTTP client for an English requirement', () => {
  const output = execFileSync(process.execPath, [
    'tools/reuse/cli.mjs', 'search', 'Send JSON requests consistently and handle server errors', '--offline', '--limit', '5',
  ], { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 });
  const result = JSON.parse(output);
  assert.equal(result.mode, 'local-semantic');
  assert.equal(result.capabilities[0].path, 'src/api/httpClient.js');
  assert.ok(result.results.some(row => row.path === 'src/api/httpClient.js' && row.symbol === 'postJson'));
});

// Real repository samples measure file recall separately from chunk ranking.
test('repository requirements report Recall@6 and Recall@12', async () => {
  const { readFileSync } = await import('node:fs');
  const { sources } = await import('./repository.mjs');
  const { searchDocuments, updateVectors, rankFiles } = await import('./search.mjs');
  const root = process.cwd();
  const cases = JSON.parse(readFileSync('tools/reuse/retrieval-cases.json', 'utf8'));
  const catalog = JSON.parse(readFileSync('.reuse/catalog.json', 'utf8'));
  const { existsSync } = await import('node:fs');
  const cache = existsSync('.cache/reuse/index.json') ? JSON.parse(readFileSync('.cache/reuse/index.json', 'utf8')) : null;
  const model = await localEmbedder(root, { offline: true });
  try {
    const index = await updateVectors(searchDocuments(sources(root), catalog), cache, model.modelKey, model.embed);
    const vectors = await model.embed(cases.map(row => row.query));
    const scores = [6, 12].map(k => ({ k, hits: cases.filter((row, i) => rankFiles(index.rows, vectors[i], k).some(item => item.path === row.path)).length, total: cases.length }));
    console.log('Repository file Recall@K:', JSON.stringify(scores));
    assert.equal(scores[1].hits, cases.length, 'Inspect failed requirements before changing samples or thresholds');
    assert.ok(scores[1].hits >= scores[0].hits);
  } finally { await model.dispose(); }
});
