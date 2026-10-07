import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { digest, safeFile } from './repository.mjs';

export function readDecisions(root) {
  const path = resolve(root, '.reuse/decisions.json');
  const ledger = existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : { version: 1, requests: [] };
  if (ledger.version !== 1 || !Array.isArray(ledger.requests)) throw new Error('Invalid decision ledger');
  const ids = new Set();
  for (const item of ledger.requests) {
    if (!item || typeof item.id !== 'string' || ids.has(item.id) || !['pending', 'resolved'].includes(item.status)
      || !['similarity', 'semantic', 'ci', 'index'].includes(item.kind) || !item.summary?.trim()
      || !Array.isArray(item.options) || item.options.length < 2 || item.options.some(x => typeof x !== 'string' || !x.trim())
      || !Array.isArray(item.evidence) || !item.evidence.length
      || item.evidence.some(x => typeof x.path !== 'string' || !/^[a-f0-9]{64}$/.test(x.hash))) throw new Error('Invalid decision ledger entry');
    if (item.scope !== undefined) validateScope(item.scope);
    ids.add(item.id);
    if (item.status === 'resolved' && (!item.decision?.trim() || !item.approvedBy?.trim() || !item.approvalRef?.trim())) throw new Error('Resolved decision is missing human approval evidence');
  }
  return ledger;
}

export function createRequest(root, { kind, summary, files, options }) {
  if (!['similarity', 'semantic', 'ci', 'index'].includes(kind) || !summary?.trim()
      || !files?.length || !options || options.length < 2 || options.some(x => !x.trim())) {
    throw new Error('pause requires --kind similarity|semantic|ci|index, --summary, --file and at least two --option values');
  }
  return { id: randomUUID(), status: 'pending', kind, summary, options, createdAt: new Date().toISOString(),
    evidence: [...new Set(files)].map(path => {
      if (!safeFile(root, path)) throw new Error(`Invalid evidence file: ${path}`);
      return { path, hash: digest(readFileSync(resolve(root, path))) };
    }) };
}

export function evidenceChanged(root, item) {
  return item.evidence.some(file => !safeFile(root, file.path) || digest(readFileSync(resolve(root, file.path))) !== file.hash);
}

export function resolveRequest(root, ledger, { id, decision, approvedBy, approvalRef }) {
  if (![id, decision, approvedBy, approvalRef].every(value => typeof value === 'string' && value.trim())) {
    throw new Error('resolve requires --id, --decision, --approved-by and --approval-ref; never invent approval');
  }
  const request = ledger.requests.find(item => item.id === id);
  if (!request || request.status !== 'pending') throw new Error('No pending request with that id');
  if (evidenceChanged(root, request)) throw new Error('Evidence changed since pause; restore it or present the updated evidence to the user before replacing the request');
  Object.assign(request, { status: 'resolved', decision, approvedBy, approvalRef, resolvedAt: new Date().toISOString() });
}

export function validateScope(scope) {
  const text = value => typeof value === 'string' && value.trim();
  if (!scope || !['files', 'callers', 'capabilityIds', 'operations'].every(key => Array.isArray(scope[key]) && scope[key].every(text))
    || ![scope.rationale, scope.reviewedBy, scope.reviewRef].every(text)
    || !(scope.files.length + scope.callers.length + scope.capabilityIds.length) || !scope.operations.length
    || [...scope.files, ...scope.callers].some(path => path.startsWith('/') || path.includes('\\') || path.split('/').some(part => !part || part === '..' || part === '.') || /[*?]/.test(path))) {
    throw new Error('Invalid decision scope: explicit files/callers/capabilities, operations and review provenance required');
  }
  return scope;
}

export function evidenceDetails(root, item) {
  return item.evidence.map(file => ({ ...file,
    reason: !safeFile(root, file.path) ? 'missing-or-unsafe'
      : digest(readFileSync(resolve(root, file.path))) !== file.hash ? 'content-changed' : 'matches',
  }));
}

export function assessPending(root, ledger, { task, changed, catalog, previousCatalog }) {
  return ledger.requests.filter(item => item.status === 'pending').map(item => {
    let reason = 'unknown-scope';
    let blocking = true;
    if (item.scope && task?.operations?.length) {
      validateScope(item.scope);
      const entries = item.scope.capabilityIds.map(id => catalog.entries.find(entry => entry.id === id));
      const related = new Set([...item.scope.files, ...item.scope.callers, ...item.evidence.map(row => row.path),
        ...entries.filter(Boolean).flatMap(entry => [entry.path, ...entry.examples, ...entry.tests])]);
      if (entries.some(entry => !entry)) reason = 'unknown-capability';
      else if (changed.includes('.reuse/catalog.json') && item.scope.capabilityIds.some(id =>
        JSON.stringify(previousCatalog?.entries?.find(entry => entry.id === id)) !== JSON.stringify(catalog.entries.find(entry => entry.id === id)))) reason = 'capability-contract-changed';
      else if (evidenceChanged(root, item)) reason = 'decision-evidence-changed';
      else if (changed.some(path => related.has(path)) || task.operations.some(op => item.scope.operations.includes(op))) reason = 'related-files-capabilities-or-operations';
      else { blocking = false; reason = 'outside-reviewed-scope'; }
    }
    return { id: item.id, blocking, reason };
  });
}
