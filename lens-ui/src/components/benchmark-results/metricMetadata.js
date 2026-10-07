/** Evaluation metric presentation metadata.
 * Client request statistics and endpoint telemetry windows have distinct keys,
 * labels and units. Moving the definitions here does not equate their samples.
 */
export const latencyFamilies = { ntpot: ['ntpot', 'Normalized TPOT'], ttft: ['ttft', 'TTFT'], itl: ['itl', 'ITL'], tpot: ['tpot', 'TPOT'], e2e: ['request_latency', 'End-to-end latency'] };
export const clientMetrics = {
    inputThroughput: ['input_throughput_tps', 'Input throughput', 'tok/s'],
    requestCount: ['request_count', 'Recorded requests', 'requests'],
    successCount: ['success_count', 'Successful requests', 'requests'],
    failureCount: ['failure_count', 'Failed requests', 'requests'],
    duration: ['benchmark_time_seconds', 'Benchmark measurement duration', 's'],
    throughput: ['throughput_tps', 'Output throughput', 'tok/s'], requestRate: ['throughput_rps', 'Completed request rate', 'req/s'], successRate: ['success_rate', 'Success rate', '%'] };
export const telemetryMetrics = {
    externalCacheHit: ['external_prefix_cache_hit_percent', 'External cache counter ratio · mean', '%'],
    cpuMemory: ['cpu_memory_usage_bytes', 'Runtime CPU memory · mean', 'bytes'],
    cacheHit: ['prefix_cache_hit_percent', 'Engine prefix counter ratio · mean', '%'],
    queue: ['queue_depth', 'Waiting requests · mean', 'requests'],
    offload: ['kv_offload_bytes_per_second', 'KV offload · mean', 'bytes/s'],
    restore: ['kv_restore_bytes_per_second', 'KV restore · mean', 'bytes/s'],
    hbmUsage: ['gpu_cache_usage_percent', 'HBM KV usage · mean', '%'],
    cpuUsage: ['cpu_cache_usage_percent', 'CPU KV usage · mean', '%'],
    transferRate: ['nixl_transfer_rate_rps', 'NIXL completed transfers · mean', 'transfers/s'],
    transferFailures: ['nixl_failed_transfer_rate_rps', 'NIXL transfer failures · mean', 'failures/s'],
    transferBandwidth: ['nixl_transfer_bytes_per_second', 'NIXL transfer bandwidth · mean', 'bytes/s'],
};
export const routerMetrics = {
    indexLookup: ['index_lookup_latency_p95_ms', 'Index lookup · mean sampled rolling P95', 'ms'],
    indexLookups: ['index_lookups_per_second', 'Index lookups · mean', 'lookups/s'],
    indexAdmissions: ['index_admissions_per_second', 'Index admissions · mean', 'blocks/s'],
    indexEvictions: ['index_evictions_per_second', 'Index evictions · mean', 'blocks/s'],
    subscribers: ['active_subscribers', 'Active subscribers · mean', 'subscribers'],
};

export const topologyMetricDefinitions = {
    request_rate_rps: ['Completed requests', 'requests/s'],
    waiting_requests: ['Waiting', 'requests'], running_requests: ['Running', 'requests'],
    inflight_token_load: ['In-flight load', 'tokens'],
    input_token_rate_tps: ['Input throughput', 'tokens/s'], output_token_rate_tps: ['Output throughput', 'tokens/s'],
    kv_cache_usage_percent: ['KV usage', '%'], gpu_cache_usage_percent: ['GPU cache usage', '%'],
    cpu_cache_usage_percent: ['CPU cache usage', '%'], gpu_utilization_percent: ['GPU utilization', '%'],
    memory_working_set_bytes: ['Memory working set', 'bytes'], cpu_memory_usage_bytes: ['CPU memory', 'bytes'],
    cpu_usage_cores: ['CPU usage', 'cores'], gpu_memory_usage_bytes: ['GPU memory', 'bytes'],
    index_lookups_per_second: ['Index lookups', 'lookups/s'], index_admissions_per_second: ['Index admissions', 'blocks/s'],
    index_evictions_per_second: ['Index evictions', 'blocks/s'], kv_event_rate_rps: ['KV events', 'events/s'],
    active_subscribers: ['Subscribers', 'subscribers'], index_lookup_latency_p95_ms: ['Index lookup P95', 'ms'],
};
export const metricDirection = {ttft:-1, itl:-1, tpot:-1, throughput:1};
export const metricColumns = {
    throughput: ['Output throughput', 'tok/s'],
    tokenLoadCv: ['Token-load CV', 'endpoint window means'],
    queue: ['Waiting requests', 'mean'],
    transferLatency: ['KV transfer latency', 'ms · mean rolling P95'],
    actualCachedPromptFraction: ['Actual engine prompt reuse', '%'],
    engineCompute: ['Engine local computation', 'tokens/s'],
    engineRecompute: ['Cached-token recomputation', 'tokens/s'],
    restore: ['KV restore', 'bytes/s · mean'],
    hbmPeak: ['HBM KV usage', '% · sampled peak'],
    hbmCapacity: ['HBM KV capacity', 'tokens · reported aggregate'],
};
