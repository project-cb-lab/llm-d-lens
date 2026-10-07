import test from 'node:test';
import assert from 'node:assert/strict';
import { applyBenchmarkPreset, benchmarkIssues, benchmarkSummary, benchmarkScenario } from './benchmarkSettings.js';

test('preset values remain editable and execution options survive preset changes', () => {
 const benchmark = applyBenchmarkPreset('throughput', {parallelism:2, wait_timeout_seconds:321});
 benchmark.matrix[0].isl = 2048;
 benchmark.concurrency_stages[0].num_requests = 123;
 const scenario = benchmarkScenario(benchmark, {success_rate_min_percent:98,ttft_ms:500,ttft_percentile:'p95'});
 assert.equal(scenario.benchmark.matrix[0].isl,2048);
 assert.equal(scenario.benchmark.concurrency_stages[0].num_requests,123);
 assert.equal(scenario.benchmark.wait_timeout_seconds,321);
 assert.equal(scenario.sla_targets.ttft_ms,500);
 assert.equal(scenario.benchmark.parallelism,2);
});
test('long input preset changes geometry while keeping load identical', () => {
 const b = applyBenchmarkPreset('long', {});
 assert.equal(b.matrix.length,3);
 assert.equal(b.concurrency_stages.length,1);
 assert.equal(benchmarkSummary(b,2).measurements,6);
});
test('invalid stages, context overflow and invalid SLO block continuation', () => {
 const b = applyBenchmarkPreset('quick', {});
 assert.deepEqual(benchmarkIssues(b,{},1024),[]);
 b.matrix[0].isl=1024;
 assert.match(benchmarkIssues(b,{},1024).join(' '),/context/i);
 b.concurrency_stages=[];
 assert.match(benchmarkIssues(b,{ttft_ms:-1},0).join(' '),/stage/i);
 assert.match(benchmarkIssues(b,{ttft_ms:-1},0).join(' '),/TTFT/);
});
test('rate duration summary counts every target, without counting dataset size as sent requests', () => {
 const b = {workload:'sanity_random.yaml',harness:'inference-perf',parallelism:1,wait_timeout_seconds:7200,shared_prefix:{num_groups:2,num_prompts_per_group:4,system_prompt_len:128,question_len:32,output_len:32,stages:[{rate:1,duration:30},{rate:2,duration:60}]}};
 assert.deepEqual(benchmarkIssues(b,{},1024),[]);
 assert.equal(benchmarkSummary(b,2).durationSeconds,180);
});

test('validates serialized fields even when their editor mode is inactive', () => {
 const b=applyBenchmarkPreset('quick',{}); b.workload='invalid/path.yaml';
 assert.match(benchmarkIssues(b,{}).join(' '),/filename/);
 b.matrix=[]; b.workload='valid.yaml'; b.warmup_requests=-1;
 assert.match(benchmarkIssues(b,{}).join(' '),/Warm-up/);
});

test('prefill overrides do not override the decode context limit', async () => {
 const {configurationContextLimit}=await import('./benchmarkSettings.js');
 assert.equal(configurationContextLimit([{deployable_configuration:{content:{decode:{maxModelLen:4096},prefill:{maxModelLen:4096},customParameters:[{name:'max-model-len',target:'prefill',value:'8192'}]}}}]),4096);
});


test('load generator memory survives preset changes and is validated', () => {
 const initial = applyBenchmarkPreset('quick');
 assert.equal(initial.harness_memory_gib, 8);
 const changed = applyBenchmarkPreset('long', {...initial, harness_memory_gib:64});
 assert.equal(benchmarkScenario(changed, {}).benchmark.harness_memory_gib, 64);
 assert.deepEqual(benchmarkIssues(changed), []);
 for (const value of [0, 513, 1.5, '']) {
  assert.ok(benchmarkIssues({...changed, harness_memory_gib:value}).some(issue => issue.includes('Load generator memory')));
 }
});
