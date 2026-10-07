import { downloadBlob } from '../../utils/download';
import yaml from 'js-yaml';
import { buildExplorerRuns } from './resultExplorer.js';

const NOTES = [
    'Model-server YAML contains only the saved model-server resources. It is not a complete deployment or benchmark recipe.',
    'The saved deployment bundle includes model-server YAML, router Helm values, source/chart metadata and recorded auxiliary resources/calibration recipes when present.',
    'Namespace, storage provisioning, model Secret references, hardware/network placement and runtime versions must match the recorded environment. Secret values and dataset contents are not supplied by this export.',
    'Post-deployment calibration may modify effective runtime settings. Saved configuration inputs do not prove that the final calibrated values or external dependencies are available.',
    'Benchmark inputs retain the recorded workload, stages and seed when available. A selected stage is context; the exported benchmark configuration describes the original case.',
];
const recordedFields = (record, keys) => Object.fromEntries(keys.filter((key) => record && Object.hasOwn(record, key)).map((key) => [key, record[key]]));
const present = (value) => typeof value === 'string' && value.trim() ? value : null;
const slug = (value) => String(value || 'benchmark').replace(/[^a-zA-Z0-9_-]/g, '-').slice(0, 100);

/** Build exports from selected case snapshots, never from live deployment records. */
export function buildResultExportState(details, run) {
    const entries = Array.isArray(details?.cases) ? details.cases : details?.evaluation ? [{ case: details.evaluation, evaluation: details.evaluation }] : [];
    const entry = entries.find((item) => String((item.case || item).id) === String(run?.caseId));
    const record = entry?.case || entry || {};
    const configuration = record.deployment_configuration || run?.configuration?.embedded || null;
    const content = configuration?.content || record.configuration || record.spec || run?.configuration || {};
    const official = content.officialGuide || {};
    const manifest = present(official.renderedManifest);
    // Derived baseline configurations must not inherit a candidate artifact reference.
    const artifactId = present(record.configuration_artifact_id);
    const benchmark = record.benchmark || content.benchmark || run?.configuration?.benchmark || {};
    const targets = record.sla_targets ?? entry?.evaluation?.sla_targets ?? content.sla_targets ?? null;
    const selection = { case_id: run?.caseId ?? null, result_id: run?.id ?? null, scope: run?.scopeLabel ?? null, stage: Boolean(run?.stage), load_kind: run?.loadKind ?? null, load: run?.load ?? null, input_tokens: run?.isl ?? null, output_tokens: run?.osl ?? null, telemetry_window: run?.window ?? null };
    return {
        benchmark, targets, results: buildExplorerRuns(details), workflowId: details?.workflow?.id,
        id: run?.id || 'benchmark', caseId: run?.caseId || 'benchmark', manifest, artifactId,
        hasEmbeddedBundle: Boolean(official.deploymentBundle),
        manifestReason: manifest ? 'Original model-server YAML from the saved configuration.' : artifactId ? 'Fetch the original saved artifact YAML.' : 'Original model-server YAML was not saved for this case.',
        bundleReason: artifactId ? 'Download the saved artifact bundle; older artifacts may contain model-server YAML only.' : official.deploymentBundle ? 'Artifact ID not recorded. Saved bundle inputs remain included in the reproduction JSON.' : 'Deployment bundle artifact was not recorded for this case.',
        reproduction: {
            schema_version: 'prism-benchmark-reproduction-inputs.v1',
            workflow: { id: details?.workflow?.id ?? null, name: details?.workflow?.name ?? null, status: details?.workflow?.status ?? null, runtime: details?.workflow?.runtime ?? null, benchmark_runtime: details?.workflow?.benchmark_runtime ?? null, cluster_session_id: details?.workflow?.cluster_session_id ?? null },
            selection,
            case: recordedFields(record, ['id', 'kind', 'baseline_type', 'baseline_parameters', 'endpoint_kind', 'endpoint', 'use_baseline_endpoint', 'sla_targets', 'dependent_guide_case_id', 'baseline_case_id', 'baseline_case_ids', 'deployment_case_id', 'deployment_run_id']),
            execution: recordedFields(entry?.evaluation, ['id', 'use_baseline_endpoint', 'endpoint_kind', 'endpoint', 'sla_targets', 'deployment_execution_id', 'namespace']),
            configuration_artifact_id: artifactId,
            configuration_checksum: configuration?.checksum ?? (artifactId ? details?.workflow?.configuration_artifacts?.[artifactId] : null) ?? null,
            deployment_configuration: configuration,
            legacy_configuration: configuration ? null : content,
            benchmark,
            benchmark_references: { evaluation_run_id: record.evaluation_run_id ?? null, output: entry?.evaluation?.output ?? null, summary_path: entry?.evaluation?.metrics?.summary_path ?? record.metrics?.summary_path ?? null, specification_file: entry?.evaluation?.specification_file ?? null },
            reproduction_notes: NOTES,
        },
        evidence: { schema_version: 'prism-benchmark-evidence.v1', selection, metrics: run?.metrics || {}, observability: run?.observability || {}, case_observability: run?.caseObservability || {}, resources: run?.resources || null, resources_scope: run?.resourcesScope ?? null },
    };
}

export async function resolveResultExport(state, kind, options = {}) {
    const prefix = slug(state.caseId);
    if (kind === 'inputs' || kind === 'targets') {
        const value = kind === 'inputs' ? state.benchmark : state.targets;
        if (!value || !Object.keys(value).length) throw new Error(`${kind === 'inputs' ? 'Benchmark inputs' : 'Performance targets'} were not recorded for this case.`);
        return { blob: new Blob([yaml.dump(value, { noRefs: true, lineWidth: -1 })], { type: 'application/yaml' }), filename: `${prefix}-${kind === 'inputs' ? 'benchmark-inputs' : 'performance-targets'}.yaml` };
    }
    if (kind === 'results') {
        const rows = state.results.filter(run => Object.keys(run.metrics).length).map(run => ({
            case_id: run.caseId, result_id: run.id, case_name: run.caseName, guide: run.guide, kind: run.kind,
            scope: run.scopeLabel, load_kind: run.loadKind, load: run.load, input_tokens: run.isl, output_tokens: run.osl,
            ...flattenMetrics(run.metrics),
        }));
        if (!rows.length) throw new Error('No benchmark results were recorded.');
        const columns = [...new Set(rows.flatMap(row => Object.keys(row)))];
        const csv = [columns, ...rows.map(row => columns.map(key => row[key]))].map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
        return { blob: new Blob([csv], { type: 'text/csv;charset=utf-8' }), filename: `${slug(state.workflowId || state.caseId)}-benchmark-results.csv` };
    }
    if (kind === 'reproduction' || kind === 'evidence') {
        const content = kind === 'reproduction' ? state.reproduction : state.evidence;
        return { blob: new Blob([JSON.stringify(content, null, 2)], { type: 'application/json' }), filename: `${prefix}-${kind === 'reproduction' ? 'reproduction-inputs' : 'evidence'}.json` };
    }
    if (kind === 'manifest' && state.manifest) return { blob: new Blob([state.manifest], { type: 'application/yaml' }), filename: `${prefix}-modelserver.yaml` };
    if (!['manifest', 'bundle'].includes(kind)) throw new Error('Unsupported export type.');
    if (!state.artifactId) throw new Error(kind === 'manifest' ? state.manifestReason : state.bundleReason);
    const fetchArtifact = options.fetch || globalThis.fetch;
    const response = await fetchArtifact(`/api/v1/configurations/artifacts/${encodeURIComponent(state.artifactId)}/${kind}`);
    if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(typeof payload.detail === 'string' ? payload.detail : `Saved artifact download failed (${response.status}).`);
    }
    const mime = response.headers.get('content-type') || '';
    if (kind === 'bundle' && !/application\/(zip|x-zip-compressed|octet-stream)/i.test(mime)) throw new Error('The artifact service did not return a deployment ZIP.');
    if (kind === 'manifest' && /text\/html|application\/json/i.test(mime)) throw new Error('The artifact service did not return model-server YAML.');
    return { blob: await response.blob(), filename: `${prefix}-${kind === 'bundle' ? 'deployment-bundle.zip' : 'modelserver.yaml'}` };
}

export function downloadResultFile({ blob, filename }) {
    downloadBlob(blob, filename);
}

function flattenMetrics(value, prefix = 'metrics', result = {}) {
    for (const [key, item] of Object.entries(value)) {
        if (key === 'observability') continue;
        const field = `${prefix}.${key}`;
        if (item && typeof item === 'object' && !Array.isArray(item)) flattenMetrics(item, field, result);
        else result[field] = Array.isArray(item) ? JSON.stringify(item) : item;
    }
    return result;
}
function csvCell(value) {
    let text = value == null ? '' : String(value);
    // Keep user-supplied labels from being interpreted as spreadsheet formulas.
    if (typeof value === 'string' && /^[=+@\-\t\r]/.test(text)) text = `'${text}`;
    return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}
