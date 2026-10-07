#!/usr/bin/env node
import { existsSync, readFileSync, mkdirSync, writeFileSync, renameSync, lstatSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { repositoryState, changedPaths, taskPath, readTask, taskErrors, reportFreshness } from './evidence.mjs';
import { createCoverage, coverageAssessment, reviewInstructions, renderReview } from './review.mjs';
import { registrationCandidates } from './registration.mjs';
import { resolve, dirname } from 'node:path';
import { parseArgs } from 'node:util';
import { git, sources, isSource, chunks, digest, validateCatalog, renderMap, duplicates } from './repository.mjs';
import { searchDocuments, updateVectors, rank, rankFiles, localEmbedder } from './search.mjs';
import { readDecisions, createRequest, resolveRequest, evidenceChanged, evidenceDetails, validateScope, assessPending } from './decisions.mjs';

function write(path, content) {
  mkdirSync(dirname(path), { recursive: true });
  const temp = `${path}.${process.pid}.tmp`;
  writeFileSync(temp, content);
  renameSync(temp, path);
}
const writeJson = (path, data) => write(path, JSON.stringify(data, null, 2) + '\n');
const readJson = path => JSON.parse(readFileSync(path, 'utf8'));

let failureReportPath;
let failureMarkdownPath;
function reportDestination(root, path) {
  const full = resolve(root, path);
  const cache = resolve(root, '.cache/reuse') + '/';
  if (!full.startsWith(cache) || !full.endsWith('.json') || full.startsWith(cache + 'tasks/')) throw new Error('Reports must be JSON files under .cache/reuse/ outside tasks/');
  for (let current = full; current !== root; current = dirname(current)) {
    if (existsSync(current) && lstatSync(current).isSymbolicLink()) throw new Error('Report path cannot traverse symlinks');
  }
  return full;
}

const HELP = `Reuse-first tools (run at any location inside the repository)
  map                         Regenerate docs/reuse-map.md from the capability catalog
  index [--offline]           Build/update local semantic file index (first run downloads model)
  search "requirement description" [--offline] [--limit 8] [--file-limit 6] [--lexical] [--task ID]
                              Semantic file retrieval then chunk ranking; lexical is explicitly separate
  check [--base REF] [--task ID] [--purpose audit|startup|task] [--json] [--report PATH]
                              Validate catalog/map/decisions; report cross-file duplicate candidates
  review --task ID            Prepare full inventory and Agent handoff (repo-review skill executes it)
  scope --task ID --module PATH  Add module coverage to an existing task without resetting base
  cover --task ID --unit ID --outcome modified|kept|blocked --summary TEXT [--id DECISION_ID]
                              Record reviewed file hashes after analysis; not proof of reading
  review-status --task ID [--json]  Cheap current coverage progress; not a completion report
  review-report --task ID     Check coverage and generate Markdown/JSON, even if incomplete
  begin --task ID [--module PATH]              Pin task baseline and create editable evidence template
  verify --task ID -- COMMAND  Execute argv without a shell; record exit code and code fingerprints
  verify-report --report PATH Reject old reports, changed inputs and unsuccessful checks
  pause --kind KIND --summary TEXT --file PATH --option TEXT --option TEXT [--scope JSON_PATH]
                              Record a human decision request; exits 2
  resolve --id ID --decision TEXT --approved-by PERSON --approval-ref REFERENCE
                              Record an actual user decision; NEVER invent approval
Exit codes: 0 checked/succeeded, 1 invalid input or deterministic failure, 2 pending human decision.
Similarity findings are advisory. Agents must analyze them and pause on uncertainty.
`;

async function main() {
  const argv = process.argv.slice(2);
  const separator = argv.indexOf('--');
  const commandArgv = argv[0] === 'verify' && separator >= 0 ? argv.slice(separator + 1) : [];
  const { values, positionals } = parseArgs({ args: argv[0] === 'verify' && separator >= 0 ? argv.slice(0, separator) : argv, allowPositionals: true, options: {
    outcome: { type: 'string' },
    module: { type: 'string', multiple: true }, unit: { type: 'string', multiple: true },
    task: { type: 'string' }, purpose: { type: 'string' }, scope: { type: 'string' }, 'file-limit': { type: 'string', default: '6' },
    base: { type: 'string' }, json: { type: 'boolean' }, report: { type: 'string' },
    offline: { type: 'boolean' }, lexical: { type: 'boolean' }, limit: { type: 'string', default: '8' },
    kind: { type: 'string' }, summary: { type: 'string' }, file: { type: 'string', multiple: true },
    option: { type: 'string', multiple: true }, id: { type: 'string' }, decision: { type: 'string' },
    'approved-by': { type: 'string' }, 'approval-ref': { type: 'string' }, help: { type: 'boolean' },
  } });
  const command = positionals[0];
  if (values.help || !command) { console.log(HELP); return; }
  if (!['map', 'index', 'search', 'check', 'pause', 'resolve', 'begin', 'verify', 'verify-report', 'review', 'review-report', 'review-status', 'scope', 'cover'].includes(command)) throw new Error(`Unknown command: ${command}`);
  const root = git(process.cwd(), ['rev-parse', '--show-toplevel']).trim();
  if (command === 'review-report') {
    taskPath(root, values.task);
    failureReportPath = reportDestination(root, `.cache/reuse/${values.task}-review.json`);
    failureMarkdownPath = resolve(root, `.cache/reuse/${values.task}-review.md`);
  }
  if (command === 'check' && values.report) failureReportPath = reportDestination(root, values.report);
  if (command === 'begin' || command === 'review') {
    if (command === 'review' && values.module) throw new Error('review requires whole-repository scope; use begin --module for a bounded module review');
    const path = taskPath(root, values.task);
    if (existsSync(path)) throw new Error('Task already exists; choose another ID or resume it');
    const baseCommit = git(root, ['rev-parse', '--verify', `${values.base || 'HEAD'}^{commit}`]).trim();
    if (git(root, ['merge-base', 'HEAD', baseCommit]).trim() !== baseCommit) throw new Error('Task baseline must be an ancestor of HEAD');
    const initialState = repositoryState(root);
    const coverage = command === 'review' || values.module ? createCoverage(root, initialState, values.module || ['.']) : undefined;
    const newTask = { version: 1, taskId: values.task, baseCommit, createdAt: new Date().toISOString(),
      initialState: initialState.fingerprint, initialFiles: initialState.files, coverage, applicableSkills: [], operations: [], searches: [], analysis: [], registrationReviews: [], verification: [] };
    writeJson(path, newTask);
    if (command === 'review') {
      write(resolve(root, `.cache/reuse/${values.task}-instructions.md`), reviewInstructions(newTask));
      console.log('Inventory prepared, not reviewed. Invoke the repo-review Agent skill to analyze and refactor all units.');
    }
    console.log(`Task evidence: ${path}. Fill skills, operations and candidate analysis; search/verify capture tool results.`);
    return;
  }
  if (command === 'verify-report') {
    if (!values.report) throw new Error('--report is required');
    const report = readJson(resolve(root, values.report));
    const failures = reportFreshness(root, report);
    if (failures.length) throw new Error(failures.join('\n'));
    console.log(`Report is current: ${report.purpose} / ${report.status}. Freshness is not approval.`);
    process.exitCode = report.status === 'INVALID' ? 1 : report.status === 'WAITING_FOR_USER' ? 2
      : ['CHECKED', 'CANDIDATES_REQUIRE_ANALYSIS', 'REGISTRATION_REVIEW_REQUIRED'].includes(report.status) ? 0 : 1;
    return;
  }
  const task = values.task ? readTask(root, values.task) : null;
  if (command === 'review-status') {
    if (!task?.coverage) throw new Error('review-status requires a task with module coverage');
    const coverage = coverageAssessment(root, task, repositoryState(root), readDecisions(root));
    const incomplete = coverage.errors.length > 0 || coverage.units.some(unit => unit.outcome === 'blocked');
    const result = { taskId: task.taskId, status: incomplete ? 'INCOMPLETE' : 'COVERAGE_RECORDED',
      completionVerified: false, coverage,
      next: `review-report --task ${task.taskId}; then verify-report --report .cache/reuse/${task.taskId}-review.json` };
    if (values.json) console.log(JSON.stringify(result, null, 2));
    else {
      console.log(`${result.status}: ${coverage.mode} scope ${coverage.scopes?.join(', ')}; ${coverage.requiredFiles.length} files, ${coverage.units.length} units`);
      for (const unit of coverage.units) console.log(`${unit.id}: ${unit.outcome} — ${unit.name}`);
      for (const error of coverage.errors) console.log(`INCOMPLETE: ${error}`);
      for (const item of coverage.excluded) console.log(`EXCLUDED: ${item.path} (${item.reason})`);
      console.log(`Coverage progress only; final checks and report freshness are still required: ${result.next}`);
    }
    process.exitCode = incomplete ? 1 : 0;
    return;
  }
  if (command === 'scope') {
    if (!task || !values.module || task.coverage) throw new Error('scope requires an existing task without coverage and --module PATH; never reset existing coverage');
    const state = repositoryState(root);
    task.coverage = createCoverage(root, state, values.module);
    if (task.initialFiles) {
      task.coverage.initialFiles = task.initialFiles;
      task.coverage.initialSnapshotKind = 'task-start';
    }
    else {
      // Legacy tasks lack a start snapshot. Conservatively include baseline changes.
      task.coverage.initialFiles = { ...state.files };
      task.coverage.initialSnapshotKind = 'baseline-fallback';
      for (const path of changedPaths(root, task.baseCommit, state)) task.coverage.initialFiles[path] = 'pre-coverage-change';
    }
    writeJson(taskPath(root, task.taskId), task);
    console.log('Module coverage added; task baseline preserved. Split units into sub-capabilities before analysis.');
    return;
  }
  if (values.unit && (!task?.coverage || values.unit.some(id => !task.coverage.units.some(unit => unit.id === id)))) throw new Error('--unit requires known task coverage units');
  if (command === 'cover') {
    if (!task?.coverage || !values.unit?.length || !['modified', 'kept', 'blocked'].includes(values.outcome) || !values.summary?.trim()) throw new Error('cover requires --task, --unit, --outcome and --summary');
    if (values.outcome === 'blocked' && !readDecisions(root).requests.some(row => row.id === values.id && row.status === 'pending')) throw new Error('Blocked unit requires an actual pending decision --id');
    const state = repositoryState(root);
    for (const unit of task.coverage.units.filter(row => values.unit.includes(row.id))) {
      unit.outcome = values.outcome;
      unit.summary = values.summary;
      unit.reviewed = Object.fromEntries(unit.files.map(path => [path, state.files[path] ?? null]));
      unit.decisionId = values.outcome === 'blocked' ? values.id : null;
    }
    writeJson(taskPath(root, task.taskId), task);
    console.log('File review recorded; check still validates per-unit searches, analysis, callers and tests.');
    return;
  }
  if (command === 'review-report') {
    if (!task?.coverage) throw new Error('review-report requires a task with module coverage');
    const reportPath = `.cache/reuse/${task.taskId}-review.json`;
    const result = spawnSync(process.execPath, [resolve(import.meta.dirname, 'cli.mjs'), 'check', '--task', task.taskId, '--json', '--report', reportPath], { cwd: root, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    if (result.error) throw result.error;
    const report = result.stdout ? JSON.parse(result.stdout) : readJson(resolve(root, reportPath));
    write(resolve(root, `.cache/reuse/${task.taskId}-review.md`), renderReview(task, report));
    console.log(`Review report: .cache/reuse/${task.taskId}-review.md (${report.status})`);
    process.exitCode = result.status ?? 1;
    return;
  }
  if (command === 'verify') {
    if (!task || !commandArgv.length) throw new Error('verify requires --task ID -- COMMAND [ARGS]');
    const before = repositoryState(root).fingerprint;
    const result = spawnSync(commandArgv[0], commandArgv.slice(1), { cwd: root, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
    const exitCode = result.status ?? 1;
    const entry = { unitIds: values.unit || [], argv: commandArgv, recordedAt: new Date().toISOString(), before,
      after: repositoryState(root).fingerprint, exitCode, signal: result.signal || null };
    const logPath = `.cache/reuse/tasks/${task.taskId}-verification-${Date.now()}.log`;
    write(resolve(root, logPath), (result.stdout || '') + (result.stderr || '') + (result.error?.message || ''));
    entry.log = logPath;
    entry.logHash = digest(readFileSync(resolve(root, logPath)));
    task.verification.push(entry);
    writeJson(taskPath(root, task.taskId), task);
    console.log(`Verification exit ${exitCode}; log: ${logPath}`);
    process.exitCode = exitCode;
    return;
  }
  const ledger = readDecisions(root);
  if (command === 'pause' || command === 'resolve') {
    if (command === 'pause') {
      const request = createRequest(root, { kind: values.kind, summary: values.summary, files: values.file, options: values.option });
      if (values.scope) request.scope = validateScope(readJson(resolve(root, values.scope)));
      if (values.id) {
        const old = ledger.requests.find(item => item.id === values.id);
        if (!old || old.status !== 'pending') throw new Error('Only a pending request can be revised');
        const { history = [], ...snapshot } = old;
        delete old.scope;
        Object.assign(old, request, { id: values.id, history: [...history, snapshot] });
        request.id = values.id;
      } else ledger.requests.push(request);
      console.log(JSON.stringify({ status: 'WAITING_FOR_USER', request }, null, 2));
      process.exitCode = 2;
    } else {
      resolveRequest(root, ledger, { id: values.id, decision: values.decision, approvedBy: values['approved-by'], approvalRef: values['approval-ref'] });
      console.log('User decision recorded. This records supplied evidence; it does not authenticate the approver.');
    }
    writeJson(resolve(root, '.reuse/decisions.json'), ledger);
    return;
  }
  const catalog = readJson(resolve(root, '.reuse/catalog.json'));
  const errors = validateCatalog(root, catalog);
  if (command === 'map') {
    if (errors.length) throw new Error(errors.join('\n'));
    write(resolve(root, 'docs/reuse-map.md'), renderMap(catalog));
    console.log('Updated docs/reuse-map.md; capability meaning still requires source/caller review.');
    return;
  }
  const files = sources(root);
  if (command === 'check') {
    const mapPath = resolve(root, 'docs/reuse-map.md');
    if (!errors.length && (!existsSync(mapPath) || readFileSync(mapPath, 'utf8') !== renderMap(catalog))) errors.push('Capability map is stale/missing: run npm run reuse:map');
    const purpose = values.purpose || (task ? 'task' : 'audit');
    if (!['audit', 'startup', 'task'].includes(purpose) || (purpose === 'task' && !task) || (task && purpose !== 'task')) throw new Error('Task purpose requires --task; audit/startup cannot claim task evidence');
    const state = repositoryState(root);
    let base = null, requestedCommit = null;
    const requestedBase = values.base || task?.baseCommit || null;
    if (requestedBase) {
      requestedCommit = git(root, ['rev-parse', '--verify', `${requestedBase}^{commit}`]).trim();
      base = git(root, ['merge-base', 'HEAD', requestedCommit]).trim();
      if (task && (requestedCommit !== task.baseCommit || base !== task.baseCommit)) errors.push('Task baseline must match the pinned ancestor commit');
    }
    const paths = changedPaths(root, base, state);
    const changed = new Set(files.filter(file => paths.includes(file.path)).map(file => file.path));
    const oldFiles = base ? sources(root, base) : [];
    const registration = registrationCandidates(files, oldFiles, catalog, task?.registrationReviews);
    const pending = ledger.requests.filter(item => item.status === 'pending');
    let previousCatalog = null;
    if (base && paths.includes('.reuse/catalog.json')) {
      try { previousCatalog = JSON.parse(git(root, ['show', `${base}:.reuse/catalog.json`])); } catch { /* missing historical catalog: conservatively unknown */ }
    }
    const pendingAssessment = assessPending(root, ledger, { task, changed: paths, catalog, previousCatalog });
    for (const item of pendingAssessment) {
      if (Array.isArray(task?.coverage?.units) && task.coverage.units.some(unit => unit?.outcome === 'blocked' && unit.decisionId === item.id)) {
        item.blocking = true; item.reason = 'blocked-review-unit';
      }
    }
    const blockingPending = pending.filter(item => pendingAssessment.find(row => row.id === item.id).blocking);
    const staleDecisions = ledger.requests.filter(item => item.status === 'resolved' && evidenceChanged(root, item))
      .map(item => ({ id: item.id, decision: item.decision, evidence: evidenceDetails(root, item),
        meaning: 'Historical decision retained; changed evidence cannot authorize a new operation.' }));
    const coverage = task ? coverageAssessment(root, task, state, ledger) : null;
    if (task && !coverage && paths.filter(isSource).length >= 10) errors.push('Large change (10+ source files) requires module coverage; use begin --module PATH for new tasks or add coverage to this task without resetting its baseline');
    if (coverage) errors.push(...coverage.errors);
    if (task) {
      errors.push(...taskErrors(root, task, state));
      if (registration.some(row => row.disposition === 'unreviewed')) errors.push('Public capability candidates need registration or hash-bound exclusion reasons');
    }
    const duplicateScope = task?.coverage?.mode === 'repository' ? 'repository' : 'changed-sources';
    const candidates = duplicates(files, duplicateScope === 'repository' ? new Set(files.map(file => file.path)) : changed);
    const report = { version: 2, generatedAt: new Date().toISOString(), purpose, requestedBase, requestedCommit, base,
      state, task: task ? { id: task.taskId, hash: digest(JSON.stringify(task)) } : null,
      status: errors.length ? 'INVALID' : blockingPending.length ? 'WAITING_FOR_USER' : candidates.length ? 'CANDIDATES_REQUIRE_ANALYSIS'
        : registration.some(row => row.disposition === 'unreviewed') ? 'REGISTRATION_REVIEW_REQUIRED' : 'CHECKED',
      coverage, duplicateScope, filesScanned: files.length, changedFiles: changed.size, changedPaths: paths, errors, pending, blockingPending, pendingAssessment,
      staleDecisionIds: staleDecisions.map(x => x.id), staleDecisions, registrationCandidates: registration, candidates,
      policy: 'Evidence is auditable, not authenticated. Unknown decision scope blocks; unrelated reviewed scope remains visible. Similarity and registration discovery are advisory, not exhaustive. Historical decisions are not blanket authorization.' };
    if (values.report) writeJson(failureReportPath, report);
    if (values.json) console.log(JSON.stringify(report, null, 2));
    else {
      console.log(`${purpose} check (${requestedBase || 'all sources'}): ${report.status}: ${files.length} source files, ${changed.size} changed, ${candidates.length} candidate pairs`);
      for (const error of errors) console.log(`ERROR: ${error}`);
      for (const request of pending) console.log(`${blockingPending.includes(request) ? 'WAITING_FOR_USER' : 'UNRELATED_PENDING'} ${request.id}: ${request.summary}`);
      console.log(`Registration: ${registration.filter(row => row.disposition === 'unreviewed').length} unreviewed candidates; use --json for details.`);
      for (const item of candidates.slice(0, 30)) console.log(`CANDIDATE ${item.left.path}:${item.left.line} <-> ${item.right.path}:${item.right.line}`);
      if (candidates.length > 30) console.log('Showing first 30 pairs; use --json or --report for all evidence.');
      if (staleDecisions.length) console.log(`${staleDecisions.length} historical approvals no longer match current evidence; do not reuse them.`);
      console.log(report.policy);
    }
    process.exitCode = errors.length ? 1 : blockingPending.length ? 2 : 0;
    return;
  }
  if (errors.length) throw new Error(errors.join('\n'));
  const query = positionals.slice(1).join(' ').trim();
  const limit = Number(values.limit);
  const fileLimit = Number(values['file-limit']);
  if (!Number.isInteger(fileLimit) || fileLimit < 1 || fileLimit > 100) throw new Error('--file-limit must be an integer from 1 to 100');
  const outputSearch = result => {
    if (task) {
      task.searches.push({ unitIds: values.unit || [], query, mode: result.mode, fileLimit, limit, recordedAt: new Date().toISOString(), state: repositoryState(root).fingerprint, paths: [...new Set(result.results.map(row => row.path))] });
      writeJson(taskPath(root, task.taskId), task);
    }
    console.log(JSON.stringify(result, null, 2));
  };
  if (!Number.isInteger(limit) || limit < 1 || limit > 50) throw new Error('--limit must be an integer from 1 to 50');
  if (command === 'search' && !query) throw new Error('search requires a query');
  if (values.lexical) {
    if (command !== 'search') throw new Error('--lexical is only valid for search');
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = files.flatMap(file => chunks(file.path, file.text)).map(row => ({ ...row,
      score: terms.filter(term => `${row.path}\n${row.text}`.toLowerCase().includes(term)).length,
    })).filter(row => row.score).sort((a, b) => b.score - a.score).slice(0, limit);
    outputSearch({ mode: 'lexical-not-semantic', fileLimit, scope: 'Whitespace substring matching across all source chunks; file-limit only affects semantic search.', results: rows });
    return;
  }
  console.error(values.offline ? 'Loading cached local embedding model (offline).' : 'Loading local embedding model; first run downloads pinned model assets. Source stays local.');
  const model = await localEmbedder(root, { offline: values.offline });
  try {
    const cachePath = resolve(root, '.cache/reuse/index.json');
    const cache = existsSync(cachePath) ? readJson(cachePath) : null;
    const index = await updateVectors(searchDocuments(files, catalog), cache, model.modelKey, model.embed,
      (done, total) => { if (done % 80 === 0 || done === total) console.error(`Indexed ${done}/${total} changed summaries`); });
    writeJson(cachePath, index);
    if (command === 'index') { console.log(`Indexed ${files.length} source files and ${catalog.entries.length} capability descriptions; ${model.modelKey}`); return; }
    const [vector] = await model.embed([query]);
    const fileResults = rankFiles(index.rows, vector, fileLimit);
    const selected = new Set(fileResults.map(row => row.path));
    const detailDocs = files.filter(file => selected.has(file.path)).flatMap(file => chunks(file.path, file.text))
      .map(row => ({ ...row, id: `${row.path}:${row.line}`, text: `${row.path} ${row.symbol}\n${row.text.slice(0, 1600)}` }));
    const detailPath = resolve(root, '.cache/reuse/details.json');
    const details = await updateVectors(detailDocs, existsSync(detailPath) ? readJson(detailPath) : null, model.modelKey, model.embed);
    writeJson(detailPath, details);
    outputSearch({ mode: 'local-semantic', model: model.modelKey, fileLimit,
      scope: `All source summaries plus capability descriptions; chunks within the top ${fileLimit} distinct files. Scores are similarity, not proof of equivalence.`,
      capabilities: rank(index.rows.filter(row => row.id.startsWith('capability:')), vector, Math.min(limit, 3)),
      files: fileResults.map(({ text: _text, ...row }) => row), results: rank(details.rows, vector, limit) });
  } finally { await model.dispose(); }
}

main().catch(error => {
  if (failureMarkdownPath) write(failureMarkdownPath, `# INCOMPLETE\n\nReview could not finish: ${error.message}\n`);
  if (failureReportPath) writeJson(failureReportPath, { version: 2, status: 'INVALID', generatedAt: new Date().toISOString(), errors: [error.message] });
  console.error(`reuse: ${error.message}`); process.exitCode = 1;
});
