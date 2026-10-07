import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import EvaluationTaskCard from './EvaluationTaskCard.jsx';

const render = task => renderToStaticMarkup(<EvaluationTaskCard task={task} onOpen={() => {}} onAction={() => {}} />);
const workflow = { id: 'private-task-id', kind: 'workflow', name: 'PD comparison', status: 'succeeded', created_at: '2026-09-09T10:00:00Z', finished_at: '2026-09-09T10:02:00Z', cases: [{ id: 'p', status: 'succeeded', configuration: { guide: 'pd-disaggregation', model: 'Qwen/test' }, benchmark: { workload: 'chat.yaml', matrix: [{ isl: 1024, osl: 128 }], concurrency_stages: [{ concurrency: 8, num_requests: 20 }] }, metrics: { throughput_tps: 420, ttft_ms: 11, latency_distributions: { ttft: { p95_ms: 28 } } } }] };

test('previous interrupted attempts show their reason inside the owning task', () => {
    const html = render({ ...workflow, status: 'running', previousBenchmarkFailures: [{ id: 'old', error: 'evaluation process was interrupted by a service restart' }] });
    assert.match(html, /Benchmark attempt history/);
    assert.match(html, /Previous benchmark interrupted by an evaluation service restart/);
    assert.match(html, /task status above reflects the current attempt/);
    assert.match(html, />running</);
    assert.doesNotMatch(html, /Previous benchmark attempt failed:/);
    assert.match(html, /PD comparison/);
    assert.doesNotMatch(html, /Endpoint benchmark|Existing endpoint/);
});

test('compact card prioritizes identity and recorded summaries over administrative details', () => {
    const html = render(workflow);
    assert.match(html, /PD comparison/);
    assert.match(html, /Qwen\/test/);
    assert.match(html, /PD disaggregation/);
    assert.match(html, /Token-length matrix/);
    assert.doesNotMatch(html, /chat.yaml/);
    assert.match(html, /420/);
    assert.match(html, /TTFT P95/);
    assert.match(html, /28/);
    assert.match(html, /2m/);
    assert.ok(html.indexOf('private-task-id') > html.indexOf('<details'));
    assert.doesNotMatch(html, /Deploy generated YAML|TTFT mean/);
});

test('missing metrics are not fabricated and flat median is never rendered as P95', () => {
    const html = render({ kind: 'workflow', id: 'empty', status: 'queued', cases: [{ configuration: { model: { name: 'model-object' } }, metrics: { ttft_ms: 987 } }] });
    assert.doesNotMatch(html, /987|100%|1\/1/);
    assert.match(html, /model-object/);
    assert.match(html, /Cancel task/);
    assert.doesNotMatch(html, /Delete task|Retry task/);
});

test('separate saved summaries display ranges without pooling percentile or throughput', () => {
    const html = render({ ...workflow, cases: [...workflow.cases, { status: 'failed', kind: 'baseline', metrics: { throughput_tps: 200, latency_distributions: { ttft: { p95_ms: 18 } } } }] });
    assert.match(html, /200–420/);
    assert.match(html, /18–28/);
    assert.match(html, /Saved summary range/);
    assert.doesNotMatch(html, />620<|>23</);
});

test('workflow retry and endpoint action permissions preserve task status semantics', () => {
    assert.match(render({ ...workflow, status: 'failed' }), /Retry task/);
    assert.match(render({ ...workflow, status: 'failed' }), /Delete task/);
    const endpoint = render({ kind: 'benchmark', id: 'endpoint', status: 'failed', model: 'Model', metrics: { throughput_tps: 0 } });
    assert.match(endpoint, /Delete task/);
    assert.doesNotMatch(endpoint, /Retry task|Open results/);
    assert.match(endpoint, /Existing endpoint/);
});

test('shared-prefix generator overrides dormant workload names and reports offered rate stages', () => {
    const html = render({ kind: 'workflow', status: 'running', id: 'prefix', workload: 'sanity_random.yaml', parallelism: 99, cases: [{ benchmark: { workload: 'sanity_random.yaml', workload_yaml: 'also ignored', concurrency_stages: [{ concurrency: 99 }], shared_prefix: { stages: [{ rate: 1, duration: 30 }, { rate: 4, duration: 60 }, { rate: 8, duration: 60 }] } } }] });
    assert.match(html, /Shared-prefix traffic/);
    assert.match(html, /Offered 1–8 req\/s/);
    assert.match(html, /3 rate stages/);
    assert.doesNotMatch(html, /sanity_random.yaml|Custom YAML|>C 99</);
});

test('matrix takes priority over prefix and YAML while endpoint custom YAML supersedes filename', () => {
    const matrix = render({ ...workflow, cases: [{ benchmark: { workload: 'unused.yaml', matrix: [{ isl: 10, osl: 5 }], shared_prefix: { stages: [{ rate: 100, duration: 1 }] }, workload_yaml: 'ignored', concurrency_stages: [{ concurrency: 8 }] } }] });
    assert.match(matrix, /Token-length matrix/);
    assert.match(matrix, />C 8</);
    assert.doesNotMatch(matrix, /Shared-prefix traffic|Custom YAML|unused.yaml|req\/s/);
    const custom = render({ kind: 'benchmark', status: 'queued', id: 'custom', workload: 'unused.yaml', workload_yaml: 'load: {}', concurrency_stages: [{ concurrency: 50 }] });
    assert.match(custom, /Custom YAML/);
    assert.doesNotMatch(custom, /unused.yaml|>C 50</);
    assert.match(render({ kind: 'benchmark', status: 'queued', id: 'named', workload: 'used.yaml' }), /used.yaml/);
});

test('active endpoint benchmarks show indeterminate progress without inventing a percentage', () => {
    const html = render({ kind: 'benchmark', id: 'running', status: 'running' });
    assert.match(html, /role="progressbar"/);
    assert.match(html, /benchmark-progress/);
    assert.doesNotMatch(html, /aria-valuenow/);
    assert.doesNotMatch(render({ kind: 'benchmark', id: 'done', status: 'succeeded' }), /benchmark-progress/);
});

test('running workflows animate within the existing progress bar and retain completed counts', () => {
    const html = render({ ...workflow, status: 'running', cases: [workflow.cases[0], { status: 'benchmarking' }] });
    assert.equal((html.match(/role="progressbar"/g) || []).length, 1);
    assert.match(html, /benchmark-progress/);
    assert.match(html, /1 \/ 2 cases/);
    assert.match(html, /aria-valuetext="Current stage: Benchmark"/);
    assert.match(html, />Deploy<|>Benchmark</);
    assert.doesNotMatch(render(workflow), /benchmark-progress/);
});

test('failed workflow keeps its failed deployment segment and error visible', () => {
    const html = render({kind:'workflow',status:'failed',cases:[{status:'failed',failed_stage:'deploying',error:'Deployment not found'}]});
    assert.equal((html.match(/role="progressbar"/g) || []).length, 1);
    assert.match(html, /Deploy · Failed/);
    assert.match(html, /Failed at: Deploy/);
    assert.match(html, /Deployment not found/);
    assert.doesNotMatch(html, /benchmark-progress/);
});

test('homepage summarizes measured matrix stages instead of empty or stale case summaries', () => {
    const html = render({ ...workflow, cases: [{ status: 'succeeded', metrics: { throughput_tps: 9999 }, matrix_results: [
        { metrics: { throughput_tps: 8888 }, stage_metrics: [
            { metrics: { throughput_tps: 12.8, success_rate: 100, latency_distributions: { ttft: { p95_ms: 196.5 } } } },
            { metrics: { throughput_tps: 639.4, success_rate: 98, latency_distributions: { ttft: { p95_ms: 1898.3 } } } },
            { metrics: {} },
        ] },
    ] }] });
    assert.match(html, /12.8–639.4/);
    assert.match(html, /196.5–1,898.3/);
    assert.match(html, /98–100/);
    assert.match(html, /Saved stage range/);
    assert.doesNotMatch(html, /9,999|8,888/);
});

test('standalone rate sweeps use stage results and retain measured zeros', () => {
    const html = render({ kind: 'benchmark', status: 'succeeded', rate_stage_results: [
        { metrics: { throughput_tps: 0, success_rate: 0 } },
        { metrics: { throughput_tps: 20, success_rate: 100 } },
    ], metrics: { throughput_tps: 9999 } });
    assert.match(html, /0–20/);
    assert.match(html, /0–100/);
    assert.match(html, /Saved stage range/);
    assert.doesNotMatch(html, /9,999/);
});
