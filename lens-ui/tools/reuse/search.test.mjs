import assert from 'node:assert/strict';
import { test } from 'node:test';
import { updateVectors, rank, searchDocuments } from './search.mjs';

test('vector ranking uses cosine similarity rather than magnitude', () => {
  const rows = [{ path: 'src/right.js', vector: [1, 1] }, { path: 'src/wrong.js', vector: [100, 0] }];
  assert.equal(rank(rows, [1, 1], 1)[0].path, 'src/right.js');
  assert.throws(() => rank([{ vector: [1] }], [1, 2], 1), /dimension/);
});

test('incremental cache re-embeds changed text, removes deleted entries and invalidates on model change', async () => {
  const calls = [];
  const embed = async texts => { calls.push(...texts); return texts.map(() => [0.6, 0.8]); };
  const docs = [{ id: 'a', text: 'save JSON' }, { id: 'b', text: 'stream audio' }];
  const first = await updateVectors(docs, null, 'model-v1', embed);
  calls.length = 0;
  const next = await updateVectors([{ id: 'a', text: 'save JSON' }, { id: 'c', text: 'loading state' }], first, 'model-v1', embed);
  assert.deepEqual(calls, ['loading state']);
  assert.deepEqual(next.rows.map(x => x.id), ['a', 'c']);
  calls.length = 0;
  await updateVectors(docs, next, 'model-v2', embed);
  assert.deepEqual(calls, ['save JSON', 'stream audio']);
});

test('search documents retain catalog purpose and actual source location', () => {
  const docs = searchDocuments([{ path: 'src/a.js', text: 'export function requestJson() { return fetch(url); }' }], {
    entries: [{ path: 'src/a.js', purpose: 'Unified request errors', boundaries: 'JSON only', symbols: ['requestJson'] }],
  });
  assert.equal(docs[0].path, 'src/a.js');
  assert.match(docs[0].text, /Unified request errors/);
  assert.match(docs[0].text, /requestJson/);
});

test('curated capability meaning has its own retrieval document without unrelated implementation text', () => {
  const docs = searchDocuments([{ path: 'src/store.js', text: 'export function save() { return unrelatedFramework(); }' }], {
    entries: [{ id: 'save', path: 'src/store.js', purpose: 'Write JSON atomically to avoid partial writes.', boundaries: 'Does not do streaming', symbols: ['save'] }],
  });
  const capability = docs.find(row => row.id === 'capability:save');
  assert.ok(capability);
  assert.match(capability.text, /Write JSON atomically/);
  assert.ok(!capability.text.includes('unrelatedFramework'));
  assert.ok(!capability.text.includes('streaming'));
});

test('invalid embedding output fails rather than caching unusable vectors', async () => {
  await assert.rejects(updateVectors([{ id: 'a', text: 'hello' }], null, 'v1', async () => [[NaN]]), /embedding/);
  await assert.rejects(updateVectors([{ id: 'a', text: 'hello' }], null, 'v1', async () => []), /embedding/);
});
