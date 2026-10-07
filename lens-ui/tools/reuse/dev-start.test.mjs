import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, copyFileSync, symlinkSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';

function setup(t) {
  const root = mkdtempSync(join(tmpdir(), 'reuse-dev-start-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const put = (path, text) => { mkdirSync(resolve(root, path, '..'), { recursive: true }); writeFileSync(join(root, path), text); };
  put('src/a.js', 'export function load() { return 1; }');
  put('.reuse/catalog.json', JSON.stringify({ version: 1, entries: [{ id: 'load', path: 'src/a.js', symbols: ['load'], purpose: 'Load', boundaries: 'Local only', examples: ['src/a.js'], tests: [], testNote: 'None' }] }));
  put('docs/reuse-map.md', 'stale');
  mkdirSync(join(root, 'scripts'));
  mkdirSync(join(root, 'tools'));
  copyFileSync('scripts/dev.sh', join(root, 'scripts/dev.sh'));
  copyFileSync('scripts/storage-env.sh', join(root, 'scripts/storage-env.sh'));
  symlinkSync(resolve('tools/reuse'), join(root, 'tools/reuse'));
  execFileSync('git', ['init', '-q', root]);
  execFileSync('git', ['add', '.'], { cwd: root });
  execFileSync('git', ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'baseline'], { cwd: root });
  // Only service/environment boundaries are replaced. Real shell dispatch,
  // map generation, Git scope, validation and decision checks execute.
  const run = command => spawnSync('bash', ['-c', `
    source ./dev.sh
    ensure_node() { :; }
    ensure_node_modules() { :; }
    ensure_venv() { :; }
    cmd_stop() { echo stop >> "$ROOT_DIR/events"; }
    start_services() { echo start >> "$ROOT_DIR/events"; }
    dev_main "$1"
  `, 'test', command], { cwd: join(root, 'scripts'), encoding: 'utf8' });
  return { root, put, run };
}

test('restart from scripts regenerates the map and validates it before restarting services', t => {
  const { root, run } = setup(t);
  const result = run('restart');
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(readFileSync(join(root, 'docs/reuse-map.md'), 'utf8'), /src\/a.js/);
  assert.equal(readFileSync(join(root, 'events'), 'utf8'), 'stop\nstart\n');
  assert.equal(JSON.parse(readFileSync(join(root, '.cache/reuse/startup-report.json'), 'utf8')).status, 'CHECKED');
});

test('invalid registration stops restart before touching running services', t => {
  const { root, put, run } = setup(t);
  put('src/a.js', 'export const other = 1;');
  const result = run('restart');
  assert.equal(result.status, 1);
  assert.ok(!existsSync(join(root, 'events')));
  assert.match(result.stdout + result.stderr, /load/);
});

for (const command of ['start', 'restart']) test(`pending refactor decision warns without blocking ${command} or resolving the decision`, t => {
  const { root, run } = setup(t);
  const paused = spawnSync(process.execPath, [resolve('tools/reuse/cli.mjs'), 'pause', '--kind', 'index', '--summary', 'Unclear contract', '--file', 'src/a.js', '--option', 'Keep', '--option', 'Change'], { cwd: root, encoding: 'utf8' });
  assert.equal(paused.status, 2);
  const ledger = readFileSync(join(root, '.reuse/decisions.json'), 'utf8');
  const result = run(command);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.equal(readFileSync(join(root, 'events'), 'utf8'), command === 'restart' ? 'stop\nstart\n' : 'start\n');
  assert.match(result.stdout, /WAITING_FOR_USER/);
  assert.match(result.stdout, /continuing local startup/);
  assert.equal(readFileSync(join(root, '.reuse/decisions.json'), 'utf8'), ledger);
  assert.equal(JSON.parse(readFileSync(join(root, '.cache/reuse/startup-report.json'), 'utf8')).status, 'WAITING_FOR_USER');
  const check = spawnSync(process.execPath, [resolve('tools/reuse/cli.mjs'), 'check'], { cwd: root, encoding: 'utf8' });
  assert.equal(check.status, 2, check.stdout + check.stderr);
});
