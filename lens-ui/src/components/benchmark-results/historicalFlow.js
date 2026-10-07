import { buildExplorerRuns } from './resultExplorer.js';

const fields = {
    request_rate: 'request_rate_rps', failed_request_rate: 'failed_request_rate_rps',
    input_token_rate: 'input_token_rate_tps', output_token_rate: 'output_token_rate_tps',
    queue_length: 'waiting_requests', kv_cache_usage_perc: 'kv_cache_usage_percent',
    prefix_cache_hit_rate: 'prefix_cache_hit_percent', external_prefix_cache_hit_rate: 'external_prefix_cache_hit_percent',
};
const mean = value => Number.isFinite(value?.mean) ? value.mean : null;
function metrics(source = {}) {
    return Object.fromEntries(Object.entries(fields).map(([target, key]) => [target, mean(source[key])]));
}

/** Only persisted serving-pod evidence is attributed to individual nodes. */
export function historicalFlow(run) {
    const observation = run.caseObservability || {};
    if (!observation.window?.start || !observation.window?.end) return null;
    const components = ['prefill', 'decode'].flatMap(role => {
        const instances = (observation.per_pod || []).filter(pod => pod.role === role)
            .map(pod => ({name: pod.pod, ...metrics(pod)}));
        return instances.length ? [{role, label: role === 'prefill' ? 'Prefill' : 'Decode', present: true, instances}] : [];
    });
    const router = observation.router || {};
    if (Object.values(metrics(router)).some(Number.isFinite) || mean(observation.summary?.epp_inflight_requests) != null) {
        components.unshift({role:'epp', label:'EPP (all instances)', present:true, instances:[{
            name:'EPP (all instances)', ...metrics(router), queue_length:mean(observation.summary?.epp_inflight_requests),
        }]});
    }
    if (!components.length) return null;
    return {namespace:observation.namespace, window:observation.window, components};
}

export function historicalFlows(details) {
    const seen = new Set();
    return buildExplorerRuns(details).flatMap(run => {
        if (seen.has(run.caseId)) return [];
        seen.add(run.caseId);
        const data = historicalFlow(run);
        return data ? [{run, data}] : [];
    });
}
