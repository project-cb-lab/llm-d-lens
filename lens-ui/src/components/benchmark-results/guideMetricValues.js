// Guide mechanism evidence uses only the selected run's persisted telemetry.
const finite = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
const roles = {
    prefillQueue: ['prefill', 'waiting_requests', 'Prefill queue', 'requests'],
    decodeQueue: ['decode', 'waiting_requests', 'Decode queue', 'requests'],
    prefillRunning: ['prefill', 'running_requests', 'Prefill running requests', 'requests'],
    decodeRunning: ['decode', 'running_requests', 'Decode running requests', 'requests'],
    prefillGpu: ['prefill', 'gpu_utilization_percent', 'Prefill GPU utilization', '%'],
    decodeGpu: ['decode', 'gpu_utilization_percent', 'Decode GPU utilization', '%'],
};
const summaries = {
    engineCompute: ['engine_prompt_local_compute_tps', 'Engine local computation', 'tokens/s'],
    engineRecompute: ['engine_prompt_recomputed_tps', 'Cached-token recomputation', 'tokens/s'],
    engineLocalReuse: ['engine_prompt_local_cache_tps', 'Engine local cache reuse', 'tokens/s'],
    engineExternalReuse: ['engine_prompt_external_tps', 'Engine external KV reuse', 'tokens/s'],
    transferLatency: ['nixl_transfer_latency_p95_ms', 'KV transfer latency · mean rolling P95', 'ms'],
    restoreTime: ['kv_restore_time_seconds_per_second', 'KV restore transfer time', 's/s'],
};
export function readGuideMetric(run, key, make) {
    const obs = run?.observability || {};
    if (roles[key]) {
        const [role, field, label, unit] = roles[key];
        return {...make(obs.derived?.role_summary?.[role]?.[field], `${label} · mean`, unit, 'Prometheus · saved per-role window means'), field:`observability.derived.role_summary.${role}.${field}`};
    }
    if (summaries[key]) {
        const [field, label, unit] = summaries[key];
        return {...make(obs.summary?.[field]?.mean, label, unit, `Prometheus · ${field} · window sample mean`, 'observed', key === 'engineCompute' ? 'Local computation includes new prompt tokens; it is not all repeated computation.' : key === 'engineRecompute' ? 'Cached tokens recomputed for a forward pass; not all prompt cache misses.' : key === 'transferLatency' ? 'Mean of sampled rolling P95 values, not a global transfer P95.' : ''), field:`observability.summary.${field}.mean`};
    }
    if (key === 'actualCachedPromptFraction') {
        const rates = ['engine_prompt_local_compute_tps','engine_prompt_local_cache_tps','engine_prompt_external_tps'].map(field => finite(obs.summary?.[field]?.mean));
        const total = rates.reduce((sum,value) => sum + (value ?? 0), 0);
        const valid = rates.every(value => value !== null && value >= 0) && total > 0;
        return make(valid ? (rates[1] + rates[2]) / total * 100 : null, 'Actual engine prompt reuse', '%', 'vLLM prompt_tokens_by_source · window mean rates', 'calculated', 'Requires all three engine token sources in the same saved window. Namespace engine traffic includes external KV transfer; not a request hit rate or request-matched causal result.', '(local_cache_hit + external_kv_transfer) / (local_compute + local_cache_hit + external_kv_transfer) × 100');
    }
    if (key === 'actualRecomputedTokens') {
        const rate = finite(obs.summary?.engine_prompt_recomputed_tps?.mean);
        const seconds = (Date.parse(obs.window?.end) - Date.parse(obs.window?.start)) / 1000;
        return make(rate !== null && rate >= 0 && seconds > 0 ? rate * seconds : null, 'Cached-token recomputation · estimated total', 'tokens', 'vLLM prompt_tokens_recomputed_total · sampled rates', 'estimated', 'Engine counter, not router estimates. Sampled rate × window duration is approximate integration, not an exact counter delta.', 'mean(engine recomputed tokens/s) × window seconds');
    }
    if (key === 'hbmPeak') {
        const value = finite(obs.summary?.gpu_cache_usage_percent?.max) ?? finite(obs.summary?.kv_cache_usage_percent?.max);
        return make(value, 'HBM KV usage · sampled peak', '%', 'Prometheus · maximum saved KV occupancy sample', 'observed', 'Sampled peak may miss short bursts; occupancy alone does not prove KV reuse after eviction.');
    }
    if (key === 'cpuCapacity') return make(obs.cache_config?.cpu_capacity_tokens, 'CPU KV token capacity · reported aggregate', 'tokens', 'vLLM cache_config_info', 'observed', 'Reported capacity is not deduplicated effective cache capacity.');
    return null;
}
