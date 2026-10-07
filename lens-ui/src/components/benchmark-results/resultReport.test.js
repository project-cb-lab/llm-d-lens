import test from 'node:test';
import assert from 'node:assert/strict';
import { buildResultReport } from './resultReport.js';

const window = { start: '2026-09-09T00:00:00Z', end: '2026-09-09T00:01:00Z' };

test('report uses exact P95 statistics and never promotes flat latency or capacity goodput', () => {
    const markdown = buildResultReport({ workflow: { id: 'w', status: 'succeeded' }, cases: [{ case: { id: 'c', configuration: { guide: 'pd-disaggregation', model: 'model-a' }, metrics: { ttft_ms: 999, slo_goodput: 888, latency_distributions: { itl: { p95_ms: 20 }, tpot: { p95_ms: 30 } } } } }] }, 'pd-disaggregation');
    assert.match(markdown, /TTFT P95 \| Not recorded/);
    assert.match(markdown, /ITL P95 \| 20 ms/);
    assert.match(markdown, /TPOT P95 \| 30 ms/);
    assert.match(markdown, /Per-request SLO goodput \| Not recorded/);
    assert.doesNotMatch(markdown, /999|888/);
});

test('case telemetry and stage client metrics retain separate scopes and sources', () => {
    const markdown = buildResultReport({ cases: [{ case: { id: 'c', metrics: { observability: { window, router: { input_token_rate_tps: { mean: 100 }, cached_token_rate_tps: { mean: 60 } } } }, rate_stage_results: [{ rate: 10, metrics: { throughput_tps: 40 } }] } }] }, 'precise-prefix-cache-routing');
    assert.match(markdown, /Stage · client summary only/);
    assert.match(markdown, /Case · full run/);
    assert.match(markdown, /ROUTER ESTIMATE \| 60 % \| estimated/);
    assert.match(markdown, /EPP cached\/input token rate samples/);
    assert.match(markdown, /2026-09-09T00:00:00Z/);
    assert.match(markdown, /mean\(router cached token rate\)/);
});

test('saved configurations and artifact references are retained while live snapshots and unsafe verdicts are excluded', () => {
    const markdown = buildResultReport({ workflow: { id: 'w', model: 'model-a', configuration_artifacts: { config1: { id: 'saved-config' } } }, cases: [{ case: { id: 'c', status: 'failed', deployment_configuration: { provider_ref: 'tiered-prefix-cache', content: { model: 'model-a', manifest: 'kind: Deployment', routerValues: 'weight: 2' } }, benchmark: { workload: 'shared-prefix', seed: 42 }, resource_snapshot: { pods: [{ name: 'saved-pod' }] } }, evaluation: { output: '/results/c', workload_file: '/results/workload.yaml', metrics: { summary_path: '/results/summary.json' } }, deployment_cases: [{ resource_snapshot: { pods: [{ name: 'live-only-pod' }] } }] }], report: { verdict: 'DEFINITELY_CAUSAL' } }, 'tiered-prefix-cache');
    assert.match(markdown, /saved-pod/);
    assert.match(markdown, /kind: Deployment/);
    assert.match(markdown, /weight: 2/);
    assert.match(markdown, /shared-prefix/);
    assert.match(markdown, /\/results\/summary.json/);
    assert.match(markdown, /\/results\/workload.yaml/);
    assert.match(markdown, /saved-config/);
    assert.doesNotMatch(markdown, /live-only-pod|DEFINITELY_CAUSAL/);
    assert.match(markdown, /failed/);
});

test('markdown text is escaped and JSON cannot close its containing fence', () => {
    const markdown = buildResultReport({ workflow: { name: '<script>x</script>\n# injected | title' }, cases: [{ case: { id: 'c', configuration: { model: '```\n# fenced payload' }, metrics: { throughput_tps: Infinity } } }] });
    assert.doesNotMatch(markdown, /<script>|\n# injected/);
    assert.match(markdown, /&lt;script&gt;/);
    assert.match(markdown, /````json/);
    assert.match(markdown, /Output throughput \| Not recorded/);
    assert.doesNotMatch(markdown, /Infinity/);
});
