import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';

const cli = resolve('tools/reuse/cli.mjs');
function setup(t) {
  const root = mkdtempSync(join(tmpdir(), 'reuse-cli-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  execFileSync('git', ['init', '-q', root]);
  const put = (name, text) => { mkdirSync(resolve(root, name, '..'), { recursive: true }); writeFileSync(join(root, name), text); };
  put('src/a.js', 'export function load() { return 1; }');
  put('.reuse/catalog.json', JSON.stringify({ version: 1, entries: [{ id: 'load', path: 'src/a.js', symbols: ['load'], purpose: 'Load value', boundaries: 'No IO', examples: ['src/a.js'], tests: [], testNote: 'No dedicated test' }] }));
  const run = (...args) => spawnSync(process.execPath, [cli, ...args], { cwd: root, encoding: 'utf8' });
  return { root, put, run };
}

test('check rejects stale map and invalid base instead of quietly passing', t => {
  const { run, put } = setup(t);
  assert.equal(run('map').status, 0);
  assert.equal(run('check').status, 0);
  put('docs/reuse-map.md', 'old map');
  const stale = run('check');
  assert.equal(stale.status, 1, stale.stderr);
  assert.match(stale.stdout, /map/);
  assert.equal(run('check', '--base', 'does-not-exist').status, 1);
});

test('pending human decision blocks check; missing approval cannot resolve it', t => {
  const { run, root } = setup(t);
  run('map');
  const paused = run('pause', '--kind', 'similarity', '--summary', 'Retry semantics conflict', '--file', 'src/a.js', '--option', 'Extend old behavior', '--option', 'Keep separate');
  assert.equal(paused.status, 2, paused.stderr);
  const record = JSON.parse(readFileSync(join(root, '.reuse/decisions.json'), 'utf8')).requests[0];
  assert.equal(run('check').status, 2);
  assert.notEqual(run('resolve', '--id', record.id, '--decision', 'Keep separate').status, 0);
  const resolved = run('resolve', '--id', record.id, '--decision', 'Keep separate', '--approved-by', 'test-user', '--approval-ref', 'test conversation: explicit confirmation');
  assert.equal(resolved.status, 0, resolved.stderr);
  assert.equal(run('check').status, 0);
});

test('approval does not apply to changed evidence', t => {
  const { run, put, root } = setup(t);
  run('pause', '--kind', 'index', '--summary', 'Conflicting contract', '--file', 'src/a.js', '--option', 'Update contract', '--option', 'Update code');
  const record = JSON.parse(readFileSync(join(root, '.reuse/decisions.json'), 'utf8')).requests[0];
  put('src/a.js', 'export function load() { throw new Error(); }');
  const result = run('resolve', '--id', record.id, '--decision', 'Update contract', '--approved-by', 'test-user', '--approval-ref', 'confirmed');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /changed/);
});

test('malformed approval ledger cannot silently bypass a pending review', t => {
  const { run, put } = setup(t);
  run('map');
  put('.reuse/decisions.json', JSON.stringify({ version: 1, requests: [{ status: 'resolved' }] }));
  assert.equal(run('check').status, 1);
});

test('revising changed evidence preserves history and still requires a user decision', t => {
  const { run, put, root } = setup(t);
  run('map');
  run('pause', '--kind', 'index', '--summary', 'Original conflict', '--file', 'src/a.js', '--option', 'Change code', '--option', 'Change contract');
  const before = JSON.parse(readFileSync(join(root, '.reuse/decisions.json'), 'utf8')).requests[0];
  put('src/a.js', 'export function load() { return 2; }');
  const updated = run('pause', '--id', before.id, '--kind', 'index', '--summary', 'Updated evidence for user', '--file', 'src/a.js', '--option', 'Change code', '--option', 'Change contract');
  assert.equal(updated.status, 2, updated.stderr);
  const after = JSON.parse(readFileSync(join(root, '.reuse/decisions.json'), 'utf8')).requests[0];
  assert.equal(after.history[0].summary, 'Original conflict');
  assert.notEqual(after.evidence[0].hash, before.evidence[0].hash);
  assert.equal(run('check').status, 2);
});

test('base comparison includes untracked implementations without flooding unchanged files', t => {
  const { root, run, put } = setup(t);
  run('map');
  execFileSync('git', ['add', '.'], { cwd: root });
  execFileSync('git', ['-c', 'user.name=Reuse Test', '-c', 'user.email=reuse@example.invalid', 'commit', '-qm', 'baseline'], { cwd: root });
  const before = run('check', '--base', 'HEAD', '--json');
  assert.equal(before.status, 0, before.stderr);
  assert.equal(JSON.parse(before.stdout).changedFiles, 0);
  put('src/new.js', 'export const extra = () => 2;');
  const after = run('check', '--base', 'HEAD', '--json');
  assert.equal(after.status, 0, after.stderr);
  assert.equal(JSON.parse(after.stdout).changedFiles, 1);
});

function baseline(root, run) {
  run('map');
  execFileSync('git', ['add', '.'], { cwd: root });
  execFileSync('git', ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'baseline'], { cwd: root });
}

test('reports bind all Git-visible inputs and reject obsolete reports', t => {
  const { root, run, put } = setup(t);
  baseline(root, run);
  const result = run('check', '--base', 'HEAD', '--purpose', 'startup', '--report', '.cache/reuse/report.json', '--json');
  assert.equal(result.status, 0, result.stderr);
  const report = JSON.parse(result.stdout);
  assert.equal(report.purpose, 'startup');
  assert.equal(report.requestedBase, 'HEAD');
  assert.equal(run('verify-report', '--report', '.cache/reuse/report.json').status, 0);
  put('src/a.test.js', '// changed test coverage');
  assert.equal(run('verify-report', '--report', '.cache/reuse/report.json').status, 1);
  put('.cache/reuse/legacy.json', JSON.stringify({ status: 'CHECKED' }));
  assert.equal(run('verify-report', '--report', '.cache/reuse/legacy.json').status, 1);
});

test('task evidence reviews public additions and invalidates verification after changes', t => {
  const { root, run, put } = setup(t);
  baseline(root, run);
  put('.agents/skills/workflow/SKILL.md', 'workflow');
  assert.equal(run('begin', '--task', 'feature').status, 0);
  assert.equal(run('begin', '--task', 'feature').status, 1);
  put('src/new.js', 'export const extra = () => 2;');
  assert.equal(run('search', 'load', '--lexical', '--task', 'feature').status, 0);
  const path = join(root, '.cache/reuse/tasks/feature.json');
  const task = JSON.parse(readFileSync(path, 'utf8'));
  task.applicableSkills = ['.agents/skills/workflow/SKILL.md'];
  task.operations = ['add-extra'];
  task.analysis = [{ capability: 'extra', candidates: ['src/a.js'], selectedApproach: 'new', rationale: 'load has a different return contract' }];
  writeFileSync(path, JSON.stringify(task));
  let result = run('check', '--task', 'feature', '--json');
  assert.equal(result.status, 1);
  let report = JSON.parse(result.stdout);
  assert.ok(report.registrationCandidates.some(x => x.symbol === 'extra' && x.disposition === 'unreviewed'));
  task.registrationReviews = report.registrationCandidates.filter(x => x.disposition === 'unreviewed').map(x => ({ key: x.key, hash: x.hash, reason: 'One-off fixture for this task, not a shared capability' }));
  writeFileSync(path, JSON.stringify(task));
  assert.equal(run('verify', '--task', 'feature', '--', process.execPath, '-e', 'process.exit(0)').status, 0);
  result = run('check', '--task', 'feature', '--report', '.cache/reuse/report.json', '--json');
  assert.equal(result.status, 0, result.stderr + result.stdout);
  assert.equal(run('verify-report', '--report', '.cache/reuse/report.json').status, 0);
  const verified = JSON.parse(readFileSync(path, 'utf8'));
  const logPath = join(root, verified.verification[0].log);
  const originalLog = readFileSync(logPath);
  writeFileSync(logPath, 'corrupted evidence');
  assert.equal(run('verify-report', '--report', '.cache/reuse/report.json').status, 1);
  writeFileSync(logPath, originalLog);
  assert.equal(run('verify', '--task', 'feature', '--', process.execPath, '-e', 'process.exit(3)').status, 3);
  assert.equal(run('check', '--task', 'feature').status, 1);
  assert.equal(run('verify-report', '--report', '.cache/reuse/report.json').status, 1);
  put('src/new.js', 'export const extra = () => 3;');
  report = JSON.parse(run('check', '--task', 'feature', '--json').stdout);
  assert.ok(report.registrationCandidates.some(x => x.symbol === 'extra' && x.disposition === 'unreviewed'));
});

test('explicit scoped pending distinguishes unrelated tasks but legacy scope stays blocking', t => {
  const { root, run, put } = setup(t);
  baseline(root, run);
  run('begin', '--task', 'feature');
  const scope = { files: ['src/a.js'], capabilityIds: ['load'], callers: [], operations: ['change-load'], rationale: 'Caller and capability review complete', reviewedBy: 'test reviewer', reviewRef: 'fixture review' };
  put('.cache/reuse/scope.json', JSON.stringify(scope));
  const paused = run('pause', '--kind', 'index', '--summary', 'load ownership', '--file', 'src/a.js', '--scope', '.cache/reuse/scope.json', '--option', 'Keep', '--option', 'Remove');
  assert.equal(paused.status, 2, paused.stderr);
  const path = join(root, '.cache/reuse/tasks/feature.json');
  const task = JSON.parse(readFileSync(path, 'utf8')); task.operations = ['unrelated-operation'];
  writeFileSync(path, JSON.stringify(task));
  let report = JSON.parse(run('check', '--task', 'feature', '--json').stdout);
  assert.equal(report.pending.length, 1);
  assert.equal(report.blockingPending.length, 0);
  put('src/a.js', 'export function load() { return 3; }');
  report = JSON.parse(run('check', '--task', 'feature', '--json').stdout);
  assert.equal(report.blockingPending.length, 1);
  assert.match(report.pendingAssessment[0].reason, /changed|related/);
  const ledger = JSON.parse(readFileSync(join(root, '.reuse/decisions.json'), 'utf8'));
  delete ledger.requests[0].scope;
  put('.reuse/decisions.json', JSON.stringify(ledger));
  report = JSON.parse(run('check', '--task', 'feature', '--json').stdout);
  assert.equal(report.blockingPending.length, 1);
});

test('historical evidence reports missing and changed files without reopening decisions', t => {
  const { root, run, put } = setup(t);
  baseline(root, run);
  const paused = JSON.parse(run('pause', '--kind', 'index', '--summary', 'remove file', '--file', 'src/a.js', '--option', 'Keep', '--option', 'Remove').stdout);
  run('resolve', '--id', paused.request.id, '--decision', 'Remove', '--approved-by', 'reviewer', '--approval-ref', 'fixture');
  put('src/a.js', 'export function load() { return 2; }');
  let report = JSON.parse(run('check', '--base', 'HEAD', '--json').stdout);
  assert.equal(report.staleDecisions[0].evidence[0].reason, 'content-changed');
  rmSync(join(root, 'src/a.js'));
  report = JSON.parse(run('check', '--base', 'HEAD', '--json').stdout);
  assert.equal(report.staleDecisions[0].evidence[0].reason, 'missing-or-unsafe');
  assert.ok(report.changedPaths.includes('src/a.js'));
  assert.equal(report.pending.length, 0);
});

test('file recall bound is validated even in lexical mode and disclosed', t => {
  const { run } = setup(t);
  assert.equal(run('search', 'load', '--lexical', '--file-limit', '0').status, 1);
  const result = run('search', 'load', '--lexical', '--file-limit', '12');
  assert.equal(result.status, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).fileLimit, 12);
});

test('failed check replaces old report and report destinations cannot overwrite sources', t => {
  const { root, run } = setup(t);
  baseline(root, run);
  run('check', '--base', 'HEAD', '--report', '.cache/reuse/report.json');
  assert.equal(run('check', '--base', 'missing-ref', '--report', '.cache/reuse/report.json').status, 1);
  assert.equal(JSON.parse(readFileSync(join(root, '.cache/reuse/report.json'), 'utf8')).status, 'INVALID');
  assert.equal(run('verify-report', '--report', '.cache/reuse/report.json').status, 1);
  assert.equal(run('check', '--report', 'src/a.js').status, 1);
  assert.match(readFileSync(join(root, 'src/a.js'), 'utf8'), /export function load/);
});

test('scoped capability contract and caller changes remain blocked', t => {
  const { root, run, put } = setup(t);
  put('src/caller.js', 'import {load} from "./a.js"; load();');
  baseline(root, run);
  run('begin', '--task', 'scope');
  const path = join(root, '.cache/reuse/tasks/scope.json');
  const task = JSON.parse(readFileSync(path, 'utf8')); task.operations = ['unrelated'];
  writeFileSync(path, JSON.stringify(task));
  put('.cache/reuse/scope.json', JSON.stringify({ files: ['src/a.js'], callers: ['src/caller.js'], capabilityIds: ['load'], operations: ['change-load'], rationale: 'Reviewed all callers', reviewedBy: 'reviewer', reviewRef: 'fixture' }));
  run('pause', '--kind', 'index', '--summary', 'ownership', '--file', 'src/a.js', '--scope', '.cache/reuse/scope.json', '--option', 'Keep', '--option', 'Remove');
  let report = JSON.parse(run('check', '--task', 'scope', '--json').stdout);
  assert.equal(report.blockingPending.length, 0);
  put('src/caller.js', 'import {load} from "./a.js"; export const value = load();');
  report = JSON.parse(run('check', '--task', 'scope', '--json').stdout);
  assert.equal(report.blockingPending.length, 1);
  const catalog = JSON.parse(readFileSync(join(root, '.reuse/catalog.json'), 'utf8'));
  catalog.entries[0].boundaries = 'New ownership contract';
  put('.reuse/catalog.json', JSON.stringify(catalog));
  report = JSON.parse(run('check', '--task', 'scope', '--json').stdout);
  assert.equal(report.pendingAssessment[0].reason, 'capability-contract-changed');
});


test('executable mode changes invalidate reports and belong to changed scope', t => {
  const { root, run, put } = setup(t);
  put('scripts/run.sh', '#!/bin/sh\nexit 0\n');
  chmodSync(join(root, 'scripts/run.sh'), 0o755);
  baseline(root, run);
  run('check', '--base', 'HEAD', '--report', '.cache/reuse/report.json');
  chmodSync(join(root, 'scripts/run.sh'), 0o644);
  assert.equal(run('verify-report', '--report', '.cache/reuse/report.json').status, 1);
  const report = JSON.parse(run('check', '--base', 'HEAD', '--json').stdout);
  assert.ok(report.changedPaths.includes('scripts/run.sh'));
});

test('repository review inventories beyond six files and reports incomplete work', t => {
  const { root, run, put } = setup(t);
  for (let i = 0; i < 8; i++) put(`src/module${i}/main.js`, `export const value${i} = ${i};`);
  put('scripts/run.sh', '#!/bin/sh\nexit 0');
  put('README.md', 'documentation');
  baseline(root, run);
  const started = run('review', '--task', 'whole');
  assert.equal(started.status, 0, started.stderr);
  const task = JSON.parse(readFileSync(join(root, '.cache/reuse/tasks/whole.json'), 'utf8'));
  assert.equal(task.coverage.mode, 'repository');
  assert.ok(Object.keys(task.coverage.inventory).length > 6);
  assert.ok(Object.hasOwn(task.coverage.inventory, 'scripts/run.sh'));
  assert.ok(Object.hasOwn(task.coverage.inventory, 'README.md'));
  assert.match(readFileSync(join(root, '.cache/reuse/whole-instructions.md'), 'utf8'), /refactor/);
  assert.equal(run('review', '--task', 'whole').status, 1, 'must not overwrite existing evidence');
  const checked = JSON.parse(run('check', '--task', 'whole', '--json').stdout);
  assert.ok(checked.coverage.errors.length > 0);
  assert.equal(run('review-report', '--task', 'whole').status, 1);
  const report = readFileSync(join(root, '.cache/reuse/whole-review.md'), 'utf8');
  assert.match(report, /INCOMPLETE/);
  assert.match(report, /src\/module7\/main.js/);
});

test('module coverage requires unit-specific evidence and detects new and changed files', t => {
  const { root, run, put } = setup(t);
  put('.agents/skills/workflow/SKILL.md', 'workflow');
  baseline(root, run);
  assert.equal(run('begin', '--task', 'module', '--module', 'src').status, 0);
  const path = join(root, '.cache/reuse/tasks/module.json');
  let task = JSON.parse(readFileSync(path, 'utf8'));
  const unit = task.coverage.units[0];
  task.applicableSkills = ['.agents/skills/workflow/SKILL.md'];
  task.operations = ['review-src'];
  task.analysis = [{ unitId: unit.id, capability: 'load', candidates: ['src/a.js'], callers: ['src/a.js'], selectedApproach: 'reuse', rationale: 'Same contract; retain existing implementation' }];
  unit.outcome = 'kept'; unit.summary = 'Existing implementation is already reusable';
  unit.reviewed = { ...task.coverage.inventory };
  writeFileSync(path, JSON.stringify(task));
  run('search', 'load', '--lexical', '--task', 'module');
  run('verify', '--task', 'module', '--', process.execPath, '-e', 'process.exit(0)');
  let report = JSON.parse(run('check', '--task', 'module', '--json').stdout);
  assert.ok(report.coverage.errors.some(x => /search/.test(x)));
  run('search', 'load', '--lexical', '--task', 'module', '--unit', unit.id);
  run('verify', '--task', 'module', '--unit', unit.id, '--', process.execPath, '-e', 'process.exit(0)');
  report = JSON.parse(run('check', '--task', 'module', '--json').stdout);
  assert.deepEqual(report.coverage.errors, []);
  assert.equal(run('review-report', '--task', 'module').status, 0);
  put('src/new.js', 'export const added = 1;');
  report = JSON.parse(run('check', '--task', 'module', '--json').stdout);
  assert.ok(report.coverage.errors.some(x => /src\/new.js/.test(x)));
  put('src/a.js', 'export function load() { return 2; }');
  report = JSON.parse(run('check', '--task', 'module', '--json').stdout);
  assert.ok(report.coverage.errors.some(x => /stale/.test(x)));
});

test('review report refreshes to incomplete when catalog or task becomes unreadable', t => {
  const { root, run, put } = setup(t);
  baseline(root, run);
  run('review', '--task', 'broken');
  put('.cache/reuse/broken-review.md', '# REVIEWED old result');
  put('.reuse/catalog.json', '{');
  assert.equal(run('review-report', '--task', 'broken').status, 1);
  assert.match(readFileSync(join(root, '.cache/reuse/broken-review.md'), 'utf8'), /INCOMPLETE/);
  put('.cache/reuse/tasks/broken.json', '{');
  put('.cache/reuse/broken-review.md', '# REVIEWED old result');
  assert.equal(run('review-report', '--task', 'broken').status, 1);
  assert.match(readFileSync(join(root, '.cache/reuse/broken-review.md'), 'utf8'), /INCOMPLETE/);
});

test('late module scope retains earlier task changes and cannot reset coverage', t => {
  const { root, run, put } = setup(t);
  put('docs/small.md', 'small document');
  baseline(root, run);
  run('begin', '--task', 'late');
  put('src/a.js', 'export function load() { return 5; }');
  assert.equal(run('scope', '--task', 'late', '--module', 'docs/small.md').status, 0);
  const report = JSON.parse(run('check', '--task', 'late', '--json').stdout);
  assert.ok(report.coverage.requiredFiles.includes('src/a.js'));
  assert.equal(run('scope', '--task', 'late', '--module', 'docs/small.md').status, 1);
});

test('large source deletion requires coverage even with no surviving changed source', t => {
  const { root, run, put } = setup(t);
  for (let i = 0; i < 10; i++) put(`src/obsolete${i}.js`, `export const old${i} = ${i};`);
  baseline(root, run);
  run('begin', '--task', 'remove');
  for (let i = 0; i < 10; i++) rmSync(join(root, `src/obsolete${i}.js`));
  const report = JSON.parse(run('check', '--task', 'remove', '--json').stdout);
  assert.ok(report.errors.some(error => /Large change/.test(error)));
});

test('whole repository review detects duplicates already committed before task start', t => {
  const { root, run, put } = setup(t);
  const body = 'export function collect(input) { let total = 0; ' + Array.from({ length: 20 }, (_, i) => `total += input[${i}] || 0;`).join(' ') + ' return total; }';
  put('src/first.js', body);
  put('src/second.js', body.replaceAll('collect', 'sum').replaceAll('total', 'result'));
  baseline(root, run);
  run('review', '--task', 'duplicates');
  const report = JSON.parse(run('check', '--task', 'duplicates', '--json').stdout);
  assert.equal(report.changedFiles, 0);
  assert.equal(report.duplicateScope, 'repository');
  assert.ok(report.candidates.some(row => row.left.path === 'src/first.js' && row.right.path === 'src/second.js'));
});

test('review-status exposes live gaps without claiming completion or writing a final report', t => {
  const { root, run, put } = setup(t);
  baseline(root, run);
  assert.equal(run('review', '--task', 'narrow', '--module', 'src').status, 1);
  assert.equal(run('review', '--task', 'progress').status, 0);
  const taskPath = join(root, '.cache/reuse/tasks/progress.json');
  const before = readFileSync(taskPath, 'utf8');
  put('llm_d_bench/new_domain/service.py', 'def load(): return 1\n');
  let result = run('review-status', '--task', 'progress', '--json');
  assert.equal(result.status, 1, result.stderr);
  let status = JSON.parse(result.stdout);
  assert.equal(status.completionVerified, false);
  assert.equal(status.coverage.mode, 'repository');
  assert.ok(status.coverage.errors.some(x => x.includes('Unassigned review file: llm_d_bench/new_domain/service.py')));
  assert.equal(readFileSync(taskPath, 'utf8'), before, 'progress must not mark files reviewed');
  assert.throws(() => readFileSync(join(root, '.cache/reuse/progress-review.json')), /ENOENT/);
  put('src/a.js', 'export function load() { return 2; }');
  result = run('review-status', '--task', 'progress', '--json');
  status = JSON.parse(result.stdout);
  assert.ok(status.coverage.errors.some(x => /stale|not reviewed/.test(x)));
});

test('recorded module coverage is explicitly distinct from final task validation', t => {
  const { root, run } = setup(t);
  baseline(root, run);
  run('begin', '--task', 'progress', '--module', 'src');
  const path = join(root, '.cache/reuse/tasks/progress.json');
  const task = JSON.parse(readFileSync(path, 'utf8'));
  const unit = task.coverage.units[0];
  task.analysis = [{ unitId: unit.id, capability: 'load', candidates: ['src/a.js'], callers: ['src/a.js'], selectedApproach: 'reuse', rationale: 'retain contract' }];
  writeFileSync(path, JSON.stringify(task));
  run('search', 'load', '--lexical', '--task', 'progress', '--unit', unit.id);
  run('cover', '--task', 'progress', '--unit', unit.id, '--outcome', 'kept', '--summary', 'retain load');
  run('verify', '--task', 'progress', '--unit', unit.id, '--', process.execPath, '--check', 'src/a.js');
  const result = run('review-status', '--task', 'progress', '--json');
  assert.equal(result.status, 0, result.stderr);
  const status = JSON.parse(result.stdout);
  assert.equal(status.status, 'COVERAGE_RECORDED');
  assert.equal(status.coverage.mode, 'module');
  assert.deepEqual(status.coverage.scopes, ['src']);
  assert.equal(status.completionVerified, false);
  assert.equal(run('review-report', '--task', 'progress').status, 1, 'missing task-level evidence must still fail final validation');
  rmSync(join(root, 'src/a.js'));
  const deleted = JSON.parse(run('review-status', '--task', 'progress', '--json').stdout);
  assert.equal(deleted.status, 'INCOMPLETE');
  assert.ok(deleted.coverage.requiredFiles.includes('src/a.js'));
});
