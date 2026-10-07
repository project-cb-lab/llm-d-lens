import { readResultMetric } from './resultExplorer.js';
import { comparisonName } from '../../features/evaluation/domain.js';
const present = value => value ?? '—';
export function overviewCases(details) {
    return (details?.cases || (details?.evaluation ? [{case:{...details.workflow,...details.evaluation}}] : [])).map(entry => {
        const record = entry.case || entry;
        const config = {...(record.spec || {}), ...(record.configuration || {}), ...(record.deployment_configuration?.content || {})};
        const decode = config.decode || config.serving || {};
        const prefill = config.prefill || {};
        const d = decode.replicaCount ?? decode.replicas ?? config.decode_replicas ?? config.replicas;
        const dtp = decode.tensorParallelSize ?? decode.tensor_parallel_size ?? config.decode_tensor_parallel_size ?? config.tensor_parallel_size;
        const p = prefill.replicaCount ?? prefill.replicas ?? config.prefill_replicas;
        const ptp = prefill.tensorParallelSize ?? prefill.tensor_parallel_size ?? config.prefill_tensor_parallel_size;
        const snapshot = record.resource_snapshot;
        const accelerator = record.metrics?.accelerator_profile || config.accelerator || snapshot?.accelerator;
        const device = snapshot?.gpu_telemetry?.devices?.find(device => device.memory_total_bytes > 0);
        const memory = device ? `${Math.round(device.memory_total_bytes / 1024**3)} GiB/card` : '';
        const hardware = typeof accelerator === 'string' ? accelerator : accelerator?.model || accelerator?.name;
        const guide = record.deployment_configuration?.provider_ref || config.guide || record.guide || 'Guide';
        return { id: String(record.id), plan: record.benchmark_plan_id || record.configuration_artifact_id || '—',
            label: record.name || (record.kind === 'baseline' ? comparisonName(record.baseline_type) : guide),
            model: typeof config.model === 'string' ? config.model : config.model?.name || details?.workflow?.model || 'Not recorded',
            topology: p != null ? `${present(p)}P × TP${present(ptp)} / ${present(d)}D × TP${present(dtp)}` : `${present(d)} serving replicas × TP${present(dtp)}`,
            gpus: d != null && dtp != null && (p == null || ptp != null) ? d * dtp + (p || 0) * (ptp || 0) : null,
            hardware: [hardware, memory].filter(Boolean).join(' · ') || 'Not recorded',
            nodes: [...new Set((snapshot?.pods || []).map(pod => pod.node).filter(Boolean))].join(', ') || 'Not recorded',
            runtime: config.runtime?.image || config.image || 'Not recorded',
            status: record.status || 'unknown', config,
            settings: [`Context ${present(decode.maxModelLen ?? config.model?.maxModelLen)}`, `Max sequences ${present(decode.maxNumSeqs)}`, ...(config.customParameters || []).filter(p => ['dtype','gpu-memory-utilization','block-size'].includes(p.name)).map(p => `${p.name}: ${p.value}`)].join(' · '),
        };
    });
}
/** Never connect rates to concurrency, different length pairs, or case summaries to stages. */
export function overviewSeries(runs) {
    const stageCases = new Set(runs.filter(run => run.stage || run.load != null).map(run => run.caseId));
    const groups = new Map();
    for (const run of runs) {
        if (run.load == null && stageCases.has(run.caseId)) continue;
        const key = JSON.stringify([run.loadKind,run.isl,run.osl]);
        if (!groups.has(key)) groups.set(key,{key, loadKind:run.loadKind, isl:run.isl, osl:run.osl, cases:[], rows:[], runs:[]});
        const group=groups.get(key);
        group.runs.push(run);
        if (!group.cases.includes(run.caseId)) group.cases.push(run.caseId);
        // Distinct repeats at the same load stay separate; no silent averaging or overwrites.
        let row=group.rows.find(row => row.x === (run.load ?? run.caseId) && !Object.hasOwn(row, `${run.caseId}:run`));
        if (!row) {row={x:run.load ?? run.caseId};group.rows.push(row);}
        row[`${run.caseId}:run`]=run.id;
        for (const metric of ['throughput','ttft','tpot']) row[`${run.caseId}:${metric}`]=readResultMetric(run,metric,'p95').value;
    }
    return [...groups.values()].map(group=>({...group, rows:group.loadKind ? group.rows.sort((a,b)=>a.x-b.x) : group.rows}));
}

export function overviewSlo(run, details) {
    const entry = (details?.cases || []).find(entry => String((entry.case || entry).id) === run.caseId);
    const targets = (entry?.case || entry || details?.evaluation)?.sla_targets || {};
    const checks = [['ttft','ttft_ms',false],['tpot','tpot_ms',false],['successRate','success_rate_min_percent',true],['throughput','throughput_min_tps',true]]
        .filter(([,field])=>typeof targets[field] === 'number')
        .map(([key,field,minimum])=>{const actual=readResultMetric(run,key,targets[`${key}_percentile`] || 'p99').value; return actual == null ? null : minimum ? actual >= targets[field] : actual <= targets[field];});
    return checks.includes(false) ? 'Not met' : checks.includes(null) ? 'Not measured' : checks.length ? 'Met' : 'Measure only';
}
