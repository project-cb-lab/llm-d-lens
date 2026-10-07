import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { discover, chunks, validateCatalog, renderMap, duplicates, digest } from './repository.mjs';

export function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'reuse-test-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  execFileSync('git', ['init', '-q', root]);
  const put = (path, text) => {
    mkdirSync(join(root, path, '..'), { recursive: true });
    writeFileSync(join(root, path), text);
  };
  return { root, put };
}

test('discovery includes new source but excludes ignored files, tests and generated output', t => {
  const { root, put } = fixture(t);
  put('.gitignore', 'src/private.js\n');
  for (const name of ['src/live.js', 'server/new.ts', 'llm_d_bench/store.py', 'src/private.js', 'src/a.test.js', 'tools/reuse/semantic.smoke.mjs', 'llm_d_bench/test_a.py', 'dist/a.js', 'server/mcp/tools.ts']) put(name, '');
  assert.deepEqual(discover(root), ['llm_d_bench/store.py', 'server/new.ts', 'src/live.js']);
});

test('chunk labels identify the containing function rather than a local variable', () => {
  const text = `export function requestJson() {\n  const response = fetch(url);\n${'  handle(response);\n'.repeat(50)}  return response;\n}`;
  const entries = chunks('src/api.js', text);
  assert.ok(entries.every(row => row.symbol === 'requestJson'));
});

test('chunks preserve symbol locations and cover the end of large functions', () => {
  const text = `export function load() {\n${'  consume(value);\n'.repeat(90)}}\nexport const save = () => 1;`;
  const entries = chunks('src/api.js', text);
  assert.ok(entries.some(x => x.symbol === 'load' && x.line === 1));
  assert.ok(entries.some(x => x.text.includes('export const save')));
  assert.ok(entries.some(x => x.endLine >= 90));
  assert.ok(chunks('llm_d_bench/store.py', 'class Store:\n    def load(self):\n        return 1\n').some(x => x.symbol === 'load'));
});

test('catalog rejects missing declarations, missing references and paths outside source roots', t => {
  const { root, put } = fixture(t);
  put('src/api.js', '// missing is mentioned but not declared\nexport function load() {}');
  const entry = { id: 'json', path: 'src/api.js', symbols: ['load'], purpose: 'Read JSON', boundaries: 'No streaming', examples: ['src/api.js'], tests: [], testNote: 'No dedicated tests yet' };
  assert.deepEqual(validateCatalog(root, { version: 1, entries: [entry] }), []);
  assert.match(validateCatalog(root, { version: 1, entries: [{ ...entry, symbols: ['missing'] }] }).join(), /missing/);
  assert.match(validateCatalog(root, { version: 1, entries: [{ ...entry, examples: ['src/gone.js'] }] }).join(), /gone/);
  assert.ok(validateCatalog(root, { version: 1, entries: [{ ...entry, path: '../outside.js' }] }).length);
  assert.ok(validateCatalog(root, { version: 1, entries: [entry, entry] }).length);
});

test('Python catalog validation does not accept declarations inside docstrings', t => {
  const { root, put } = fixture(t);
  put('llm_d_bench/store.py', '"""Example:\ndef missing():\n    pass\n"""\ndef real():\n    return 1\n');
  const entry = { id: 'python', path: 'llm_d_bench/store.py', symbols: ['missing'], purpose: 'Load', boundaries: 'Local', examples: ['llm_d_bench/store.py'], tests: [], testNote: 'None' };
  assert.match(validateCatalog(root, { version: 1, entries: [entry] }).join(), /missing/);
});

test('duplicate evidence catches identifier renaming and reports source locations', () => {
  const a = 'export function load(input) { const result = []; for (const item of input) { if (item.active) { result.push(item.value); } } return result; }';
  const b = a.replaceAll('load', 'collect').replaceAll('input', 'items').replaceAll('result', 'output');
  const files = [{ path: 'src/a.js', text: a }, { path: 'src/b.js', text: b }, { path: 'src/c.js', text: 'export const answer = 42;' }];
  const hits = duplicates(files, new Set(['src/b.js']), { minTokens: 30 });
  assert.equal(hits.length, 1);
  assert.deepEqual(new Set([hits[0].left.path, hits[0].right.path]), new Set(['src/a.js', 'src/b.js']));
  assert.equal(hits[0].left.line, 1);
  assert.equal(duplicates(files, new Set(['src/c.js']), { minTokens: 30 }).length, 0);
});

test('map rendering is deterministic and tracks changes to capability boundaries', () => {
  const entry = { id: 'json', path: 'src/a.js', symbols: ['load'], purpose: 'JSON', boundaries: 'No streams', examples: ['src/b.js'], tests: [], testNote: 'None' };
  const first = renderMap({ version: 1, entries: [entry] });
  assert.equal(first, renderMap({ version: 1, entries: [entry] }));
  assert.notEqual(digest(first), digest(renderMap({ version: 1, entries: [{ ...entry, boundaries: 'Streams too' }] })));
});
