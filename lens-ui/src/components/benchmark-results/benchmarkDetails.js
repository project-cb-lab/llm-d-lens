/** Adapt a standalone run to the workflow detail shape without changing ownership. */
export function benchmarkDetails(run = {}) {
    const text = [run.stdout, run.harness_logs].filter(Boolean).join('\n');
    const read = (label) => text.match(new RegExp(`^\\s*${label}:\\s*(.+)$`, 'im'))?.[1]?.trim() || '';
    const benchmark = run.benchmark || Object.fromEntries([
        'harness', 'workload', 'parallelism', 'matrix', 'concurrency_stages',
        'shared_prefix', 'workload_yaml', 'warmup_requests', 'wait_timeout_seconds',
    ].filter(key => run[key] != null).map(key => [key, run[key]]));
    const enriched = {
        ...run,
        deployment_ownership: run.deployment_ownership || 'existing-endpoint',
        model: run.model || run.model_name || read('Model') || text.match(/(?:model_name|model)\s*[=:]\s*["']?([^,\s}"']+)/i)?.[1] || '',
        harness: run.harness || read('Harness'),
        workload: run.workload || read('Workload'),
        namespace: run.namespace || read('Namespace'),
        benchmark,
    };
    enriched.configuration = {
        model: enriched.model, guide: run.guide,
        ...run.configuration, benchmark,
    };
    return {
        workflow: enriched,
        evaluation: enriched,
        cases: [{ case: enriched, evaluation: enriched, deployment_cases: run.deployment_cases || [] }],
    };
}