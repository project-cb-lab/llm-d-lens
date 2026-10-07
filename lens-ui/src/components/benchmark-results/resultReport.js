import { buildExplorerRuns, readResultMetric, GUIDE_EXPLORER_PROFILES } from './resultExplorer.js';

const COMMON_KEYS = ['ttft', 'itl', 'tpot', 'e2e', 'throughput', 'inputThroughput', 'requestRate', 'successRate', 'requestCount', 'successCount', 'failureCount', 'duration', 'goodput'];
const escape = (value) => String(value ?? 'Not recorded').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replace(/[\\`*_[\]{}|#]/g, '\\$&').replace(/[\r\n]+/g, ' ');
const valueText = (metric) => typeof metric.value === 'number' && Number.isFinite(metric.value) ? `${Number(metric.value.toPrecision(8))}${metric.unit ? ` ${metric.unit}` : ''}` : 'Not recorded';
const jsonBlock = (value) => {
    const json = JSON.stringify(value, (_, item) => typeof item === 'number' && !Number.isFinite(item) ? null : item, 2);
    const longest = Math.max(2, ...(json.match(/`+/g) || []).map((match) => match.length));
    const fence = '`'.repeat(longest + 1);
    return `${fence}json\n${json}\n${fence}`;
};

function metricTable(run, keys) {
    return [
        '| Metric | Value | Quality | Source | Scope | Formula / qualification |',
        '|---|---:|---|---|---|---|',
        ...keys.map((key) => {
            const metric = readResultMetric(run, key, 'p95');
            return `| ${[metric.label, valueText(metric), metric.quality, metric.source, metric.scope, [metric.formula, metric.reason].filter(Boolean).join(' · ') || '—'].map(escape).join(' | ')} |`;
        }),
    ];
}

function artifactReferences(value, prefix = '', result = []) {
    if (!value || typeof value !== 'object') return result;
    for (const [key, item] of Object.entries(value)) {
        // Gather stored references, not commands, logs or inferred output locations.
        if (/^(summary_path|output|workload_file|workload_path|specification_file|report_path|configuration_artifact_id|evaluation_run_id|deployment_run_id)$/.test(key) && typeof item === 'string') result.push([`${prefix}${key}`, item]);
        if (key === 'artifacts' || key === 'configuration_artifacts' || key === 'artifact_refs') result.push([`${prefix}${key}`, item]);
        if (['metrics', 'rate_stage_results', 'matrix_results', 'stage_metrics'].includes(key) && item && typeof item === 'object') artifactReferences(item, `${prefix}${key}.`, result);
        if (/^\d+$/.test(key) && item && typeof item === 'object') artifactReferences(item, `${prefix}${key}.`, result);
    }
    return result;
}

/** Export only source-aware metrics used by the result explorer, never legacy verdicts. */
export function buildResultReport(details, guideType) {
    const workflow = details?.workflow || {};
    const runs = buildExplorerRuns(details);
    const entries = Array.isArray(details?.cases) ? details.cases : details?.evaluation ? [{ case: details.evaluation, evaluation: details.evaluation }] : [];
    const lines = [
        `# ${escape(workflow.name || 'Benchmark evidence report')}`, '',
        `Evaluation: ${escape(workflow.id || details?.evaluation?.id)} · Status: ${escape(workflow.status || details?.evaluation?.status)}`, '',
        `Model: ${escape(workflow.model || [...new Set(runs.map((run) => typeof run.configuration.model === 'string' ? run.configuration.model : run.configuration.model?.name).filter(Boolean))].join(', ') || 'Not recorded')}`, '',
        `Created: ${escape(workflow.created_at)} · Finished: ${escape(workflow.finished_at)}`, '',
        'This report describes saved results and their collection scope. It makes no automatic causal verdict or resource-parity claim. Stage and case summaries are separate observations, not additional independent trials.', '',
        'ITL and TPOT are distinct. P95 is shown only when explicitly recorded. Missing values are not zero. SLO goodput cannot be reconstructed from aggregate latency quantiles or capacity-derived throughput.', '',
        '## Recorded results', '',
    ];
    if (!runs.length) lines.push('No saved benchmark results are available.', '');
    for (const run of runs) {
        const profile = GUIDE_EXPLORER_PROFILES[run.guide] || GUIDE_EXPLORER_PROFILES[guideType];
        const entry = entries.find((candidate) => String((candidate.case || candidate).id) === run.caseId);
        const record = entry?.case || entry || {};
        lines.push(`### ${escape(run.label)}`, '', `Result ID: ${escape(run.id)} · Case ID: ${escape(run.caseId)} · Case status: ${escape(record.status || entry?.evaluation?.status)}`, '', `Scope: ${escape(run.scopeLabel)}`, '');
        if (run.load !== null) lines.push(`Load: ${escape(run.load)} ${run.loadKind === 'rate' ? 'offered requests/s' : 'concurrent requests'}`, '');
        if (run.isl !== null || run.osl !== null) lines.push(`Input length: ${escape(run.isl)} tokens · Output length: ${escape(run.osl)} tokens`, '');
        lines.push(`Persisted telemetry window: ${run.window ? `${escape(run.window.start)} → ${escape(run.window.end)}` : 'Not recorded'}`, '');
        if (run.stage && run.window) lines.push('Stage telemetry uses persisted duration-aligned bounds; exact request timestamps and stage attribution were not independently verified.', '');
        lines.push(...metricTable(run, COMMON_KEYS), '');
        if (profile) {
            lines.push(`#### ${escape(profile.title)}`, '', escape(profile.question), '', escape(profile.description), '', ...metricTable(run, profile.evidenceKeys), '', 'Evidence limitations:', '', ...profile.limitations.map((item) => `- ${escape(item)}`), '');
        }
    }
    lines.push('## Saved configurations and resources', '', 'Configuration values express recorded intent. Resource snapshots below were persisted at case completion; they are not stage telemetry. Live deployment snapshots are excluded.', '');
    const seen = new Set();
    for (const run of runs) {
        if (seen.has(run.caseId)) continue;
        seen.add(run.caseId);
        lines.push(`### Case ${escape(run.caseId)}`, '', 'Saved configuration and benchmark inputs:', '', jsonBlock(run.configuration), '', `Resource source: ${escape(run.resourcesScope)}`, '');
        if (run.resources) lines.push(jsonBlock(run.resources), '');
        else lines.push(escape(run.resourcesReason), '');
    }
    lines.push('## Recorded artifact references', '', 'Paths and identifiers below are retained references; availability of the referenced files has not been checked.', '');
    const references = artifactReferences(workflow, 'workflow.');
    artifactReferences(details?.evaluation, 'evaluation.', references);
    for (const [index, entry] of entries.entries()) {
        artifactReferences(entry.case || entry, `cases.${index}.case.`, references);
        artifactReferences(entry.evaluation, `cases.${index}.evaluation.`, references);
    }
    if (!references.length) lines.push('No artifact references were saved.', '');
    else for (const [path, value] of references) lines.push(`### ${escape(path)}`, '', typeof value === 'object' && value !== null ? jsonBlock(value) : escape(value), '');
    return `${lines.join('\n').trim()}\n`;
}
