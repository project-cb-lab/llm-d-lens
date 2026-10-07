import { readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { safeFile } from './repository.mjs';

function normalizeScope(path) {
  if (typeof path !== 'string' || !path || path.startsWith('/') || path.includes('\\') || path.split('/').includes('..') || /[*?]/.test(path)) throw new Error('Module scope must be a repository-relative path');
  return path.replace(/^\.\//, '').replace(/\/$/, '') || '.';
}
const inScope = (path, scopes) => scopes.some(scope => scope === '.' || path === scope || path.startsWith(scope + '/'));

export function reviewInventory(root, state, scopes) {
  const inventory = {}, excluded = [];
  for (const [path, hash] of Object.entries(state.files)) {
    if (!inScope(path, scopes) || hash === null) continue;
    let reason;
    if (/(^|\/)(node_modules|dist|build|vendor|\.git)(\/|$)/.test(path) || path === 'server/mcp/tools.ts') reason = 'generated/dependency artifact';
    else if (!safeFile(root, path)) reason = 'symlink or non-regular file';
    else {
      const body = readFileSync(`${root}/${path}`);
      if (body.length > 1024 * 1024) reason = 'over 1 MiB: requires separate review';
      else if (body.includes(0)) reason = 'binary file';
    }
    if (reason) excluded.push({ path, reason });
    else inventory[path] = hash;
  }
  return { inventory, excluded };
}

export function createCoverage(root, state, paths = ['.']) {
  const scopes = [...new Set(paths.map(normalizeScope))];
  const { inventory, excluded } = reviewInventory(root, state, scopes);
  if (!Object.keys(inventory).length) throw new Error('Review scope has no eligible files');
  for (const scope of scopes) if (!Object.keys(inventory).some(path => inScope(path, [scope]))) throw new Error(`Empty review scope: ${scope}`);
  const groups = new Map();
  for (const path of Object.keys(inventory)) {
    const name = dirname(path);
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(path);
  }
  return { version: 1, initialSnapshotKind: 'inventory-start', initialFiles: state.files, mode: scopes.includes('.') ? 'repository' : 'module', scopes, inventory, excluded,
    units: [...groups].map(([name, files], i) => ({ id: `unit-${i + 1}`, name, files, outcome: 'unreviewed', summary: '', reviewed: {} })) };
}

export function coverageAssessment(root, task, state, ledger) {
  const coverage = task.coverage;
  if (!coverage) return null;
  const errors = [];
  if (coverage.version !== 1 || !['repository', 'module'].includes(coverage.mode) || !Array.isArray(coverage.scopes) || !coverage.scopes.length
    || !coverage.inventory || typeof coverage.inventory !== 'object' || Array.isArray(coverage.inventory) || !Array.isArray(coverage.units)) {
    return { errors: ['Invalid module coverage structure'], units: [], requiredFiles: [], excluded: [] };
  }
  let scopes;
  try { scopes = coverage.scopes.map(normalizeScope); } catch { return { errors: ['Invalid module scope'], units: [], requiredFiles: [], excluded: [] }; }
  if (coverage.mode === 'repository' && !scopes.includes('.')) errors.push('Repository review must retain whole-repository scope');
  const current = reviewInventory(root, state, scopes);
  const allCurrent = reviewInventory(root, state, ['.']);
  const taskChanges = [...new Set([...Object.keys(coverage.initialFiles || {}), ...Object.keys(state.files)])].filter(path =>
    (coverage.initialFiles?.[path] ?? null) !== (state.files[path] ?? null));
  const outsideChanges = taskChanges.filter(path => Object.hasOwn(allCurrent.inventory, path) || (state.files[path] == null && coverage.initialFiles?.[path]));
  const requiredFiles = [...new Set([...Object.keys(coverage.inventory), ...Object.keys(current.inventory), ...outsideChanges])];
  const ids = new Set(), assigned = new Set();
  const units = [];
  for (const unit of coverage.units) {
    const fail = message => errors.push(`${unit?.id || '(invalid unit)'}: ${message}`);
    if (!unit || typeof unit.id !== 'string' || !unit.id || ids.has(unit.id) || !Array.isArray(unit.files) || !unit.files.length) { fail('invalid/duplicate unit or missing files'); continue; }
    ids.add(unit.id);
    for (const path of unit.files) {
      if (!requiredFiles.includes(path)) fail(`file outside inventory: ${path}`);
      if (assigned.has(path)) fail(`file assigned twice: ${path}`);
      assigned.add(path);
      if (!Object.hasOwn(unit.reviewed || {}, path) || unit.reviewed[path] !== (state.files[path] ?? null)) fail(`stale or missing file review: ${path}`);
    }
    if (unit.outcome === 'kept' && unit.files.some(path => taskChanges.includes(path))) fail('kept outcome contradicts changes since inventory creation');
    if (!['modified', 'kept', 'blocked'].includes(unit.outcome) || typeof unit.summary !== 'string' || !unit.summary.trim()) fail('needs a completed outcome and explanation');
    if (!task.searches.some(row => row.unitIds?.includes(unit.id))) fail('missing unit-specific search');
    const analysis = task.analysis.filter(row => row.unitId === unit.id);
    if (!analysis.length || analysis.some(row => !Array.isArray(row.callers) || (!row.callers.length && !row.callerNote?.trim()) || row.callers.some(path => !safeFile(root, path)))) fail('missing unit analysis/caller evidence');
    if (unit.outcome === 'blocked') {
      if (!ledger.requests.some(row => row.id === unit.decisionId && row.status === 'pending')) fail('blocked unit must reference an actual pending decision');
    } else {
      const verification = new Map(task.verification.filter(row => row.unitIds?.includes(unit.id)).map(row => [JSON.stringify(row.argv), row]));
      if (!verification.size || [...verification.values()].some(row => row.exitCode !== 0 || row.before !== state.fingerprint || row.after !== state.fingerprint)) fail('missing, failed or stale unit verification');
    }
    units.push({ id: unit.id, name: unit.name, files: unit.files, outcome: unit.outcome, summary: unit.summary, decisionId: unit.decisionId || null });
  }
  for (const path of requiredFiles) if (!assigned.has(path)) errors.push(`Unassigned review file: ${path}`);
  return { mode: coverage.mode, taskChanges, scopes, errors, units, requiredFiles, currentHashes: Object.fromEntries(requiredFiles.map(path => [path, state.files[path] ?? null])), excluded: [...current.excluded, ...allCurrent.excluded.filter(row => taskChanges.includes(row.path) && !current.excluded.some(item => item.path === row.path))] };
}

export function reviewInstructions(task) {
  return `# Repository review and refactor: ${task.taskId}\n\n` +
    `Read .agents/skills/repo-review/SKILL.md and continue task ${task.taskId}.\n` +
    `The CLI prepared an inventory; it has NOT performed Agent analysis or refactoring.\n` +
    `Task evidence: .cache/reuse/tasks/${task.taskId}.json\n\n` +
    `Historical reports are finding lists, not the scope of a whole-repository review. Use the live root inventory.\n` +
    `Check interim coverage with review-status --task ${task.taskId}; incomplete units remain Agent work.\n` +
    `Review every unit, split directory groups into actual sub-capabilities, and preserve pre-existing edits.\n` +
    `Implement unambiguous compatible refactors, migrate callers, register capabilities and run tests.\n` +
    `Record per-unit search (--unit ID), analysis/callers, result and verification (--unit ID).\n` +
    `Never auto-resolve pending or commit/push. Pause only uncertain dependent edits; continue independent work.\n` +
    `Finish with review-report --task ${task.taskId}; disclose exclusions, pending and incomplete units.\n`;
}

export function renderReview(task, input) {
  const report = { errors: [], pending: [], blockingPending: [], changedPaths: [], generatedAt: new Date().toISOString(), base: task.baseCommit, ...input };
  if (!report.coverage) report.errors = [...report.errors, 'Coverage assessment unavailable; check did not complete'];
  const text = value => String(value ?? '').replaceAll('\n', ' ').replaceAll('`', "'").replaceAll('<', '&lt;');
  const coverage = report.coverage;
  const incomplete = report.status === 'INVALID' || report.errors.length || report.blockingPending.length || !coverage || coverage.errors.length || coverage.units.some(unit => unit.outcome === 'blocked');
  const lines = [`# Repository Review and Refactoring Report: ${text(task.taskId)}`, '', `Status: ${incomplete ? 'INCOMPLETE' : 'REVIEWED'} (${text(report.status)})`, '',
    `Baseline: ${report.base}; generated at: ${report.generatedAt}`, '',
    'This summarizes Agent records and tool checks; it does not prove every business judgment correct. Changes remain in the workspace for the user to retain or reject.', '',
    '## Coverage and outcomes', '', `Inventory: ${coverage?.requiredFiles.length || 0} files; ${coverage?.units.length || 0} capability units.`, ''];
  for (const unit of coverage?.units || []) {
    lines.push(`### ${text(unit.name || unit.id)} — ${text(unit.outcome)}`, '', text(unit.summary || 'Review not yet complete'), '');
    for (const path of unit.files) lines.push(`- ${text(path)}`);
    for (const row of task.analysis.filter(row => row.unitId === unit.id)) {
      lines.push(`- Approach: ${text(row.selectedApproach)}; rationale: ${text(row.rationale)}`);
      lines.push(`- Candidates: ${(row.candidates || []).map(text).join(', ')}; callers: ${(row.callers || []).map(text).join(', ') || text(row.callerNote)}`);
    }
    lines.push('');
  }
  lines.push(coverage?.initialSnapshotKind === 'baseline-fallback' ? '## Changes inferred from the commit baseline for a legacy task (no initial file snapshot)' : '## Files changed since the task-start snapshot', '', ...(coverage?.taskChanges || []).map(path => `- ${text(path)}`), '', 'All changes relative to the task commit baseline follow. They may include pre-existing user changes and must not all be attributed to the Agent:', '', ...report.changedPaths.map(path => `- ${text(path)}`), '', '## Verification records', '');
  for (const row of task.verification) lines.push(`- ${text(row.argv.join(' '))}: exit ${row.exitCode}；${text(row.log)}`);
  lines.push('', `## Duplicate candidates (${text(report.duplicateScope || 'unknown')})`, '');
  for (const row of report.candidates || []) lines.push(`- ${text(row.left.path)}:${row.left.line} ↔ ${text(row.right.path)}:${row.right.line} (requires per-unit analysis; does not imply mandatory consolidation)`);
  lines.push('', '## Incomplete work, pending decisions, and exclusions', '');
  for (const error of report.errors) lines.push(`- ${text(error)}`);
  for (const item of report.pending) lines.push(`- pending ${text(item.id)}: ${text(item.summary)}`);
  for (const item of coverage?.excluded || []) lines.push(`- Excluded ${text(item.path)}: ${text(item.reason)}`);
  lines.push('', `JSON check evidence: .cache/reuse/${text(task.taskId)}-review.json`, '', 'Before delivery, validate the corresponding JSON with verify-report; regenerate this report after code or evidence changes.', '');
  return lines.join('\n');
}
