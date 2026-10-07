import test from 'node:test';
import assert from 'node:assert/strict';
import { buildExplorerRuns, readResultMetric, getRunSeries, resolveExplorerGuide } from './resultExplorer.js';

const window = { start: '2026-09-09T00:00:00Z', end: '2026-09-09T00:01:00Z' };
const point = (seconds, queue_depth) => ({ timestamp: `2026-09-09T00:${seconds >= 60 ? '01' : '00'}:${String(seconds % 60).padStart(2, '0')}Z`, queue_depth });

test('stage results cannot inherit case metrics, resources or unscoped telemetry', () => {
    const [run] = buildExplorerRuns({ cases: [{ case: { id: 'a', spec: { guide: 'pd-disaggregation' }, metrics: { throughput_tps: 999, observability: { window, summary: { queue_depth: { mean: 8 } } } }, rate_stage_results: [{ rate: 10, metrics: { ttft_ms: 4 } }] } }] });
    assert.equal(readResultMetric(run, 'throughput').value, null);
    assert.equal(readResultMetric(run, 'queue').value, null);
    assert.equal(run.caseObservability.summary.queue_depth.mean, 8);
    assert.equal(run.loadKind, 'rate');
    assert.equal(run.load, 10);
});

test('own persisted stage window clips case samples and preserves missing and role values', () => {
    const [run] = buildExplorerRuns({ cases: [{ case: { id: 'a', metrics: { observability: { window, series: [point(40, 4), point(10, 1), point(20, null)], role_series: [{ ...point(20, null), prefill_waiting_requests: 6 }] } }, rate_stage_results: [{ rate: 0, metrics: {}, observability: { window: { start: point(15).timestamp, end: point(30).timestamp }, summary: {} } }] } }] });
    assert.equal(run.load, 0);
    assert.equal(run.observability.series.length, 1);
    assert.equal(run.observability.role_series.length, 1);
    assert.deepEqual(getRunSeries(run).map(({ elapsed, queue_depth, prefill_waiting_requests }) => ({ elapsed, queue_depth, prefill_waiting_requests })), [{ elapsed: 5, queue_depth: null, prefill_waiting_requests: 6 }]);
});

test('matrix stages retain their own load and lengths without merged point metrics', () => {
    const runs = buildExplorerRuns({ cases: [{ case: { id: 'm', configuration: { guide: 'optimized-baseline', benchmark: { parallelism: 99 } }, matrix_results: [{ isl: 100, osl: 20, metrics: { throughput_tps: 999 }, stage_metrics: [{ concurrency: 3, metrics: { throughput_tps: 7 } }, { metrics: {} }] }] } }] });
    assert.equal(runs[0].loadKind, 'concurrency');
    assert.equal(runs[0].isl, 100);
    assert.equal(runs[1].load, null);
    assert.equal(readResultMetric(runs[1], 'throughput').value, null);
});

test('typed configuration survives normalization and live deployment snapshots are excluded', () => {
    const [run] = buildExplorerRuns({ cases: [{ case: { id: 't', deployment_case_id: 'correct', deployment_configuration: { provider_ref: 'tiered-prefix-cache', content: { model: { name: 'model-x' }, serving: { components: { decode: { replicas: 2 } } } } }, metrics: {} }, deployment_cases: [{ id: 'wrong', resource_snapshot: { pods: ['wrong'] } }, { id: 'correct', resource_snapshot: { pods: ['correct'] } }] }] });
    assert.equal(run.guide, 'tiered-prefix-cache');
    assert.equal(run.configuration.model.name, 'model-x');
    assert.equal(run.resources, null);
    assert.match(run.resourcesReason, /live/i);
});

test('latency quantiles stay distinct from medians and ITL cannot substitute for TPOT', () => {
    const run = { metrics: { ttft_ms: 9, itl_ms: 8, latency_distributions: { ttft: { p95_ms: 24, mean_ms: 13 } } }, stage: true };
    assert.equal(readResultMetric(run, 'ttft').value, 24);
    assert.equal(readResultMetric(run, 'ttft', 'mean').value, 13);
    assert.equal(readResultMetric(run, 'ttft', 'p50').value, null);
    assert.equal(readResultMetric(run, 'ttft', 'p99').value, null);
    assert.equal(readResultMetric(run, 'tpot', 'p50').value, null);
});

test('legacy goodput and mixed cache aliases cannot masquerade as measured evidence', () => {
    const run = { metrics: { slo_goodput: 99, effective_prefix_hit_rate: 99, effective_cached_prompt_fraction: 99, recomputed_prompt_tokens: 10 }, observability: { evidence: { effective_prefix_hit_rate: { value: 99, status: 'measured' } } } };
    assert.equal(readResultMetric(run, 'goodput').value, null);
    assert.equal(readResultMetric(run, 'cacheHit').value, null);
    assert.equal(readResultMetric(run, 'cachedPromptFraction').value, null);
    assert.equal(readResultMetric(run, 'recomputedTokens').value, null);
});

test('router fractions are estimates, CV uses endpoint means, histogram samples are not global quantiles', () => {
    const run = { stage: false, observability: { window, router: { input_token_rate_tps: { mean: 100 }, cached_token_rate_tps: { mean: 60 }, index_lookup_latency_p95_ms: { mean: 4, p95: 10 } }, per_endpoint: [{ inflight_token_load: { mean: 10 } }, { inflight_token_load: { mean: 30 } }] } };
    assert.equal(readResultMetric(run, 'cachedPromptFraction').value, 60);
    assert.equal(readResultMetric(run, 'cachedPromptFraction').quality, 'estimated');
    assert.equal(readResultMetric(run, 'recomputedTokens').value, 2400);
    assert.equal(readResultMetric(run, 'tokenLoadCv').value, 0.5);
    assert.match(readResultMetric(run, 'tokenLoadCv').label, /window means/i);
    assert.equal(readResultMetric(run, 'indexLookup').value, 4);
    assert.match(readResultMetric(run, 'indexLookup').label, /sampled.*P95/i);
});

test('unavailable, nonfinite and zero values remain distinct', () => {
    assert.equal(readResultMetric({ metrics: { throughput_tps: Infinity } }, 'throughput').quality, 'unavailable');
    assert.equal(readResultMetric({ metrics: { throughput_tps: 0 } }, 'throughput').value, 0);
    assert.deepEqual(buildExplorerRuns(null), []);
    assert.deepEqual(getRunSeries({ stage: true, observability: { series: [point(10, 4)] } }), []);
});

test('typed snapshots retain separate benchmark inputs and do not borrow unmatched deployment resources', () => {
    const [run] = buildExplorerRuns({ cases: [{ case: { id: 't', deployment_case_id: 'missing', benchmark: { workload: 'shared-prefix', parallelism: 2 }, deployment_configuration: { provider_ref: 'optimized-baseline', content: { model: 'model-x' } } }, deployment_cases: [{ id: 'other', resource_snapshot: { pods: ['other'] } }] }] });
    assert.equal(run.configuration.benchmark?.workload, 'shared-prefix');
    assert.equal(run.resources, null);
    assert.equal(run.load, null);
    assert.equal(run.loadKind, null);
    assert.equal(run.configuration.benchmark.parallelism, 2);
});

test('time series sort chronologically, reject outside and invalid timestamps without mutating input', () => {
    const series = [point(40, 4), point(10, 1), point(70, 7), { timestamp: 'bad', queue_depth: 9 }];
    const run = { observability: { window, series } };
    assert.deepEqual(getRunSeries(run).map((item) => item.elapsed), [10, 40]);
    assert.equal(series[0].queue_depth, 4);
});


test('ambiguous flat latency does not manufacture mean or percentile values', () => {
    const run = { metrics: { ttft_ms: 9, latency_distributions: { ttft: { mean_ms: 12 } } } };
    assert.equal(readResultMetric(run, 'ttft', 'p50').value, null);
    assert.equal(readResultMetric(run, 'ttft', 'mean').value, 12);
    assert.equal(readResultMetric({ metrics: { ttft_ms: 9 } }, 'ttft', 'mean').value, null);
    assert.equal(readResultMetric({ metrics: { latency_distributions: { ttft: { p50_ms: 7 } } } }, 'ttft', 'p50').value, 7);
});

test('case evidence remains separately selectable alongside rate stages without a fabricated load', () => {
    const runs = buildExplorerRuns({ cases: [{ case: { id: 'r', benchmark: { parallelism: 99 }, metrics: { throughput_tps: 50, observability: { window, per_endpoint: [{ inflight_token_load: { mean: 10 } }, { inflight_token_load: { mean: 30 } }] } }, rate_stage_results: [{ rate: 10, metrics: { throughput_tps: 20 } }] } }] });
    assert.equal(runs.length, 2);
    assert.equal(runs[0].stage, true);
    const summary = runs.find((run) => !run.stage);
    assert.equal(summary?.loadKind, null);
    assert.equal(summary?.load, null);
    assert.equal(readResultMetric(summary, 'tokenLoadCv').value, 0.5);
    assert.equal(readResultMetric(runs[0], 'tokenLoadCv').value, null);
});

test('matrix case scope does not claim the latest point client summary is a global aggregate', () => {
    const runs = buildExplorerRuns({ cases: [{ case: { id: 'm', metrics: { throughput_tps: 999, observability: { window, cache_config: { hbm_capacity_tokens: 1000 } } }, matrix_results: [{ isl: 100, osl: 10, stage_metrics: [{ concurrency: 1, metrics: { throughput_tps: 20 } }] }] } }] });
    const summary = runs.find((run) => !run.stage);
    assert.ok(summary);
    assert.equal(readResultMetric(summary, 'throughput').value, null);
    assert.equal(readResultMetric(summary, 'hbmCapacity').value, 1000);
});

test('client counts, input throughput and measurement duration expose only recorded numbers', () => {
    const run = { metrics: { input_throughput_tps: 400, request_count: 12, success_count: 10, failure_count: 2, benchmark_time_seconds: 5 } };
    assert.equal(readResultMetric(run, 'inputThroughput').value, 400);
    assert.equal(readResultMetric(run, 'requestCount').value, 12);
    assert.equal(readResultMetric(run, 'successCount').value, 10);
    assert.equal(readResultMetric(run, 'failureCount').value, 2);
    assert.equal(readResultMetric(run, 'duration').value, 5);
    assert.equal(readResultMetric({ metrics: { request_count: 12, success_count: 10 } }, 'failureCount').value, null);
});

test('router estimates cannot populate requested actual reuse or actual recomputation evidence', () => {
    const run = { observability: { window, router: { input_token_rate_tps: { mean: 100 }, cached_token_rate_tps: { mean: 60 } } } };
    assert.equal(readResultMetric(run, 'cachedPromptFraction').value, 60);
    assert.equal(readResultMetric(run, 'actualCachedPromptFraction').value, null);
    assert.equal(readResultMetric(run, 'actualRecomputedTokens').value, null);
});

test('external cache counter ratio stays distinct and transfer byte integration is an estimate', () => {
    const run = { observability: { window, summary: { external_prefix_cache_hit_percent: { mean: 25 }, prefix_cache_hit_percent: { mean: 80 }, cpu_memory_usage_bytes: { mean: 1024 }, kv_offload_bytes_per_second: { mean: 10 }, kv_restore_bytes_per_second: { mean: 5 } } } };
    assert.equal(readResultMetric(run, 'externalCacheHit').value, 25);
    assert.match(readResultMetric(run, 'externalCacheHit').reason, /external.*quer/i);
    assert.equal(readResultMetric(run, 'cpuMemory').value, 1024);
    assert.equal(readResultMetric(run, 'offloadedBytes').value, 600);
    assert.equal(readResultMetric(run, 'restoredBytes').value, 300);
    assert.equal(readResultMetric(run, 'restoredBytes').quality, 'estimated');
    assert.equal(readResultMetric({ observability: { summary: run.observability.summary } }, 'restoredBytes').value, null);
});


test('persisted case resources win over refreshed live deployment snapshots', () => {
    const [run] = buildExplorerRuns({ cases: [{ case: { id: 'saved', deployment_case_id: 'live', resource_snapshot: { pods: [{ name: 'saved-pod' }] } }, deployment_cases: [{ id: 'live', resource_snapshot: { pods: [{ name: 'live-pod' }] } }] }] });
    assert.deepEqual(run.resources.pods, [{ name: 'saved-pod' }]);
    assert.match(run.resourcesScope, /persisted/i);
});


test('guide resolution prioritizes the candidate over an earlier approximate baseline', () => {
    assert.equal(resolveExplorerGuide({ cases: [
        { case: { kind: 'baseline', configuration: { guide: 'optimized-baseline' } } },
        { case: { kind: 'guide', deployment_configuration: { provider_ref: 'precise-prefix-cache-routing' }, configuration: { guide: 'optimized-baseline' } } },
    ] }), 'precise-prefix-cache-routing');
    assert.equal(resolveExplorerGuide({ cases: [
        { case: { kind: 'baseline', deployment_configuration: { provider_ref: 'optimized-baseline' } } },
        { case: { kind: 'guide', spec: { guide: 'pd-disaggregation' } } },
    ] }), 'pd-disaggregation');
    assert.equal(resolveExplorerGuide(null), 'optimized-baseline');
});

test('P90 is read exactly and evidence distinguishes fields and measurement context', () => {
  const run = { label: 'Candidate / rate 3', window, metrics: { latency_distributions: { ttft: { p90_ms: 19, p95_ms: 23 } } } };
  const metric = readResultMetric(run, 'ttft', 'p90');
  assert.equal(metric.value, 19);
  assert.equal(metric.field, 'metrics.latency_distributions.ttft.p90_ms');
  assert.match(metric.definition, /first response token/);
  assert.equal(metric.resultLabel, run.label);
  assert.deepEqual(metric.window, window);
  assert.equal(readResultMetric(run, 'ttft', 'p99').value, null);
});

test('engine reuse uses engine source rates and never router estimates', () => {
 const run={observability:{window,summary:{engine_prompt_local_compute_tps:{mean:20},engine_prompt_local_cache_tps:{mean:60},engine_prompt_external_tps:{mean:20},engine_prompt_recomputed_tps:{mean:2}}}};
 assert.equal(readResultMetric(run,'actualCachedPromptFraction').value,80);
 assert.equal(readResultMetric(run,'engineCompute').value,20);
 assert.equal(readResultMetric(run,'actualRecomputedTokens').value,120);
 assert.equal(readResultMetric(run,'actualRecomputedTokens').quality,'estimated');
 delete run.observability.summary.engine_prompt_external_tps;
 assert.equal(readResultMetric(run,'actualCachedPromptFraction').value,null);
 assert.equal(readResultMetric({observability:{router:{cached_token_rate_tps:{mean:90},input_token_rate_tps:{mean:100}}}},'actualCachedPromptFraction').value,null);
});

test('guide role metrics keep measured zero and do not inherit full-case values',()=>{
 const run={stage:true,observability:{derived:{role_summary:{prefill:{waiting_requests:0},decode:{waiting_requests:9}}}},caseObservability:{derived:{role_summary:{prefill:{waiting_requests:99}}}}};
 assert.equal(readResultMetric(run,'prefillQueue').value,0);
 assert.equal(readResultMetric(run,'decodeQueue').value,9);
 assert.equal(readResultMetric({...run,observability:{}},'prefillQueue').value,null);
 assert.equal(readResultMetric({observability:{summary:{kv_cache_usage_percent:{mean:71,max:99}}}},'hbmPeak').value,99);
});

test('request SLO evidence displays zero and never inherits case evidence into a stage', () => {
    const evidence = {schema_version:1,status:'measured',value:0,duration_seconds:60,good_requests:0,request_count:20,constraints_seconds:{ttft:0.2}};
    const runs = buildExplorerRuns({cases:[{case:{id:'slo',metrics:{request_slo_goodput:evidence},rate_stage_results:[{rate:1,metrics:{}},{rate:2,metrics:{request_slo_goodput:evidence}}]}}]});
    assert.equal(readResultMetric(runs[0],'goodput').value,null);
    const metric = readResultMetric(runs[1],'goodput');
    assert.equal(metric.value,0);
    assert.equal(metric.quality,'calculated');
    assert.deepEqual(metric.scope.constraints_seconds,{ttft:0.2});
    assert.equal(readResultMetric({metrics:{request_slo_goodput:{...evidence,status:'unavailable',reason:'Missing requests'}}},'goodput').reason,'Missing requests');
});

test('KV measured window and configured prefix scale stay separate', () => {
    const run = {configuration:{benchmark:{shared_prefix:{num_groups:8,system_prompt_len:1024}}},metrics:{distinct_kv_working_set:{schema_version:1,status:'measured',value:16,window}}};
    assert.equal(readResultMetric(run,'workingSet').value,16);
    assert.deepEqual(readResultMetric(run,'workingSet').window,window);
    assert.equal(readResultMetric(run,'configuredPrefixScale').value,8192);
    assert.equal(readResultMetric(run,'configuredPrefixScale').quality,'estimated');
    delete run.metrics.distinct_kv_working_set;
    assert.equal(readResultMetric(run,'workingSet').value,null);
});

test('matrix KV collection has a selectable point summary without leaking into stages', () => {
    const kv = {schema_version:1,status:'measured',value:32,window,measurement_scope:'completed-full-prompt-blocks'};
    const runs = buildExplorerRuns({cases:[{case:{id:'kv-matrix',configuration:{guide:'tiered-prefix-cache'},matrix_results:[
        {isl:1024,osl:128,metrics:{distinct_kv_working_set:kv},stage_metrics:[{concurrency:1,metrics:{}},{concurrency:8,metrics:{}}]}
    ]}}]});
    assert.equal(readResultMetric(runs[0],'workingSet').value,null);
    assert.equal(readResultMetric(runs[1],'workingSet').value,null);
    const full = runs.find(r=>r.id==='kv-matrix:matrix-0:summary');
    assert.equal(readResultMetric(full,'workingSet').value,32);
    assert.match(readResultMetric(full,'workingSet').label,/completed requests/);
    assert.match(full.scopeLabel,/Length point/);
    assert.deepEqual(full.observability,{});
});
