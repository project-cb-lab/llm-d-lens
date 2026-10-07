import assert from 'node:assert/strict';
import { test } from 'node:test';
import { registrationCandidates } from './registration.mjs';
import { rankFiles } from './search.mjs';

const catalog = { entries: [] };
test('registration discovery covers aliases, defaults, reexports and public Python classes', () => {
  const files = [
    { path: 'src/a.ts', text: 'const impl = () => 1; export { impl as shared }; export default function() { return 2; } export { x } from "./b"; export * from "./c";' },
    { path: 'llm_d_bench/a.py', text: 'def _private(): pass\ndef public(): pass\nclass Shared: pass\n' },
  ];
  const found = registrationCandidates(files, [], catalog);
  assert.deepEqual(found.map(x => x.symbol), ['shared', 'default', 'x', '*', 'public', 'Shared']);
  assert.equal(registrationCandidates(files, files, catalog).length, 0);
  const reviewed = found.map(x => ({ key: x.key, hash: x.hash, reason: 'domain-private export' }));
  assert.ok(registrationCandidates(files, [], catalog, reviewed).every(x => x.disposition === 'excluded'));
  const changed = [{ ...files[0], text: files[0].text.replace('=> 1', '=> 3') }];
  assert.ok(registrationCandidates(changed, files, catalog, reviewed).some(x => x.symbol === 'shared' && x.disposition === 'unreviewed'));
});

test('file retrieval expands beyond six while deduplicating capability and file rows', () => {
  const rows = Array.from({ length: 8 }, (_, i) => ({ path: `src/${i}.js`, vector: [8-i, i] }));
  rows.splice(1, 0, { path: 'src/0.js', vector: [1, 0] });
  assert.equal(rankFiles(rows, [1, 0], 6).length, 6);
  assert.ok(!rankFiles(rows, [1, 0], 6).some(row => row.path === 'src/6.js'));
  assert.ok(rankFiles(rows, [1, 0], 8).some(row => row.path === 'src/6.js'));
  assert.throws(() => rankFiles(rows, [1, 0], 0), /file-limit/);
});

test('default identifier implementation and reexport module changes require review', () => {
  for (const [before, after] of [
    ['const impl = () => 1; export default impl;', 'const impl = () => 2; export default impl;'],
    ['const impl = () => 1; export { impl } from "./old.js";', 'const impl = () => 1; export { impl } from "./new.js";'],
  ]) {
    assert.equal(registrationCandidates([{ path: 'src/a.js', text: after }], [{ path: 'src/a.js', text: before }], catalog).length, 1);
  }
});
