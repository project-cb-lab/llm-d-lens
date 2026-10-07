import { reportFreshness } from './evidence.mjs';
import { existsSync, readFileSync, appendFileSync } from 'node:fs';

const path = '.cache/reuse/report.json';
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('`', '&#96;').replaceAll('\n', ' ');
const lines = ['## Reuse checks', ''];
if (!existsSync(path)) lines.push('Report unavailable. Inspect the failed setup/test/check step; this is not a clean result.');
else {
  const report = JSON.parse(readFileSync(path, 'utf8'));
  const failures = reportFreshness(process.cwd(), report);
  if (failures.length) {
    lines.push(`Report unusable: ${failures.map(escape).join('; ')}`);
    process.exitCode = 1;
  } else {
    lines.push(`Purpose: **${escape(report.purpose)}**; base: ${escape(report.base || 'all sources')}`, '');
    lines.push(`Status: **${escape(report.status)}**`, '', `${report.filesScanned} source files; ${report.changedFiles} changed; ${report.candidates.length} candidate pairs.`, '');
    for (const error of report.errors) lines.push(`- Error: ${escape(error)}`);
    for (const item of report.blockingPending) lines.push(`- Waiting for user: ${escape(item.summary)} (${escape(item.id)})`);
    for (const item of report.candidates.slice(0, 30)) lines.push(`- Candidate: ${escape(item.left.path)}:${item.left.line} ↔ ${escape(item.right.path)}:${item.right.line}`);
    lines.push('', `${report.pending.length - report.blockingPending.length} unrelated pending decisions remain visible in the report.`, 'Full evidence is in .cache/reuse/report.json; export it with task evidence and verification logs for review. Similarity is advisory: inspect contracts and callers; pause dependent changes when uncertain.');
  }
}
const output = lines.join('\n') + '\n';
if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, output);
else console.log(output);
