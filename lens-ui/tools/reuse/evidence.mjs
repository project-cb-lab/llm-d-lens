import { readFileSync, lstatSync, readlinkSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { git, digest, safeFile } from './repository.mjs';

const regularHash = (mode, contents) => digest(Buffer.concat([Buffer.from(`${mode}\0`), contents]));

export function taskPath(root, id) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/.test(id || '')) throw new Error('Invalid task ID');
  return resolve(root, `.cache/reuse/tasks/${id}.json`);
}

export function readTask(root, id) {
  const task = JSON.parse(readFileSync(taskPath(root, id), 'utf8'));
  if (task.version !== 1 || task.taskId !== id || !/^[a-f0-9]{40,64}$/.test(task.baseCommit || '')
    || !['applicableSkills', 'operations', 'searches', 'analysis', 'registrationReviews', 'verification'].every(key => Array.isArray(task[key]))) {
    throw new Error('Invalid task evidence');
  }
  return task;
}

// Git-visible files, not only implementation sources: tests/config/skills affect evidence.
// Reuse artifacts are excluded to avoid a report hashing itself. Ignored runtime data
// is intentionally out of scope; manifests contain hashes, never file contents.
export function repositoryState(root) {
  const paths = [...new Set(git(root, ['ls-files', '-z', '--cached', '--others', '--exclude-standard']).split('\0'))]
    .filter(path => path && !path.startsWith('.cache/reuse/')).sort();
  const files = Object.fromEntries(paths.map(path => {
    const full = resolve(root, path);
    let hash = null;
    try {
      const stat = lstatSync(full);
      hash = stat.isSymbolicLink() ? digest(`symlink:${readlinkSync(full)}`)
        : stat.isFile() ? regularHash(stat.mode & 0o111 ? '100755' : '100644', readFileSync(full)) : 'non-file';
    } catch (error) { if (error.code !== 'ENOENT' && error.code !== 'ENOTDIR') throw error; }
    return [path, hash];
  }));
  let head = null;
  try { head = git(root, ['rev-parse', '--verify', 'HEAD']).trim(); } catch { /* unborn fixture repository */ }
  return { head, fingerprint: digest(JSON.stringify({ head, files })), files };
}

export function changedPaths(root, base, state) {
  if (!base) return Object.keys(state.files);
  const records = git(root, ['ls-tree', '-r', '-z', base]).split('\0').filter(Boolean);
  const old = new Map(records.map(record => {
    const [header, path] = record.split('\t');
    if (!header.startsWith('100') && !header.startsWith('120')) return [path, 'non-file'];
    const contents = execFileSync('git', ['show', `${base}:${path}`], { cwd: root, maxBuffer: 64 * 1024 * 1024 });
    return [path, header.startsWith('120') ? digest(`symlink:${contents.toString()}`) : regularHash(header.split(' ')[0], contents)];
  }));
  return [...new Set([...old.keys(), ...Object.keys(state.files)])]
    .filter(path => !path.startsWith('.cache/reuse/') && (old.get(path) ?? null) !== (state.files[path] ?? null)).sort();
}

export function taskErrors(root, task, state) {
  const errors = [];
  const text = value => typeof value === 'string' && value.trim();
  if (!task.applicableSkills.length || task.applicableSkills.some(path => !safeFile(root, path))) errors.push('Task must reference applicable skill files');
  if (!task.operations.length || task.operations.some(value => !text(value))) errors.push('Task must declare its operations');
  if (!task.searches.length || task.searches.some(row => !text(row.query) || !['lexical-not-semantic', 'local-semantic'].includes(row.mode))) errors.push('Task needs actual search evidence (search --task ID)');
  if (!task.analysis.length || task.analysis.some(row => !text(row.capability) || !Array.isArray(row.candidates)
    || !row.candidates.length || row.candidates.some(path => !safeFile(root, path))
    || !['reuse', 'adapt', 'extend', 'extract', 'new'].includes(row.selectedApproach) || !text(row.rationale))) {
    errors.push('Task needs candidate locations, approach and contract rationale');
  }
  const latest = new Map(task.verification.map(row => [JSON.stringify(row.argv), row]));
  if (!latest.size || [...latest.values()].some(row => !Array.isArray(row.argv) || !row.argv.length || row.exitCode !== 0
    || row.before !== state.fingerprint || row.after !== state.fingerprint
    || !safeFile(root, row.log) || digest(readFileSync(resolve(root, row.log))) !== row.logHash)) errors.push('Task verification missing, failed or stale; run verify --task ID -- COMMAND');
  return errors;
}

export function reportFreshness(root, report) {
  const errors = [];
  if (report.version !== 2 || !report.state?.fingerprint || !['audit', 'startup', 'task'].includes(report.purpose)) return ['Report lacks current evidence metadata; regenerate it'];
  if (repositoryState(root).fingerprint !== report.state.fingerprint) errors.push('Repository changed since this report');
  if (report.task) {
    try {
      const task = readTask(root, report.task.id);
      if (digest(JSON.stringify(task)) !== report.task.hash) errors.push('Task evidence changed since this report');
      for (const row of task.verification) {
        if (!safeFile(root, row.log) || digest(readFileSync(resolve(root, row.log))) !== row.logHash) errors.push('Verification log missing or changed');
      }
    } catch { errors.push('Task evidence missing or invalid'); }
  }
  if (report.requestedBase) {
    try {
      if (git(root, ['rev-parse', '--verify', `${report.requestedBase}^{commit}`]).trim() !== report.requestedCommit) errors.push('Requested base reference moved');
    } catch { errors.push('Requested base reference unavailable'); }
  }
  return errors;
}
