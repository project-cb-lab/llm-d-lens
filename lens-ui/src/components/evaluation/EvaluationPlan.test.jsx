import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import EvaluationPlan from './EvaluationPlan.jsx';

const defaults = {
    configurations: [
        { id: 'a', guide: 'Baseline', topology: '1 replica · TP2', gpu: 2, model: 'model-a', image: 'runtime:v1' },
        { id: 'b', guide: 'P/D', topology: '1P×TP2 / 1D×TP2', gpu: 4, model: 'model-a', image: 'runtime:v1' },
    ],
    baselines: ['direct-vllm'],
    benchmark: { matrix: [{ isl: 128, osl: 64 }, { isl: 1024, osl: 128 }], concurrency_stages: [{ concurrency: 1, num_requests: 10 }, { concurrency: 8, num_requests: 40 }], warmup_requests: 2, parallelism: 1, wait_timeout_seconds: 7200 },
    slaTargets: { success_rate_min_percent: 98, ttft_ms: 450, ttft_percentile: 'p95', tpot_ms: '' },
    cluster: 'Test cluster',
    onEditConfigurations() {}, onEditBenchmark() {}, onEditSetup() {},
};
const render = (props = {}) => renderToStaticMarkup(createElement(EvaluationPlan, { ...defaults, ...props }));

test('execution plan separates tasks from measurements and shows selected targets in execution order', () => {
    const html = render();
    assert.match(html, /3 benchmark tasks/);
    assert.match(html, /12 measured combinations/);
    assert.match(html, /300 measured requests/);
    assert.ok(html.indexOf('Baseline') < html.indexOf('P/D'));
    assert.ok(html.indexOf('P/D') < html.indexOf('Prism plain vLLM reference'));
    assert.match(html, /450 ms/);
    assert.match(html, /Measure only/);
    assert.match(html, /8 concurrent/);
    assert.match(html, /40 requests/);
    assert.doesNotMatch(html, /Comparison Validity|View Generated Plan|Run Matrix|✓/);
});

test('retention stays in the execution sequence without an extra explanatory section', () => {
    const html = render({ preserveDeployment: true });
    assert.match(html, /keep successful configuration deployments/);
    assert.doesNotMatch(html, /After execution|Evaluation results remain available in the task details/);
    assert.match(html, /clean up independent references/);
});

test('same-pod reference runs immediately after its owning configuration and before independent references', () => {
    const html = render({ baselines: ['direct-vllm', 'kubernetes-service'] });
    assert.match(html, /4 benchmark tasks/);
    assert.match(html, /16 measured combinations/);
    assert.ok(html.indexOf('Configuration A') < html.indexOf('Kubernetes Service round-robin'));
    assert.ok(html.indexOf('Kubernetes Service round-robin') < html.indexOf('Configuration B'));
    assert.ok(html.indexOf('Configuration B') < html.indexOf('Prism plain vLLM reference'));
    assert.match(html, /Reuse Configuration A pods through Kubernetes Service/);
});

test('existing endpoint ignores leftover configuration and reference selections', () => {
    const html = render({ existingEndpoint: 'https://model.example/v1' });
    assert.match(html, /1 benchmark task/);
    assert.match(html, /4 measured combinations/);
    assert.match(html, /https:\/\/model.example\/v1/);
    assert.match(html, /No deployment is created or cleaned up/);
    assert.doesNotMatch(html, /Prism plain vLLM reference|Edit configurations|TP2/);
});

test('rate stages show configured load duration without claiming total wall time', () => {
    const html = render({ baselines: [], benchmark: { shared_prefix: { num_groups: 2, num_prompts_per_group: 4, system_prompt_len: 128, question_len: 32, output_len: 16, stages: [{ rate: 1, duration: 30 }, { rate: 4, duration: 60 }] } } });
    assert.match(html, /4 measured combinations/);
    assert.match(html, /180s configured load time/);
    assert.match(html, /4 req\/s/);
    assert.match(html, /60s/);
    assert.match(html, /excludes deployment, readiness and cleanup/);
});

test('profile and YAML do not invent stage counts or estimates', () => {
    const profile = render({ benchmark: { workload: 'custom.yaml' } });
    assert.match(profile, /Repository profile: custom.yaml/);
    assert.match(profile, /Stage count and duration come from the workload/);
    assert.doesNotMatch(profile, /0 measured|configured load time/);
    const yaml = render({ benchmark: { workload_yaml: 'load:\n  type: constant' } });
    assert.match(yaml, /View workload YAML/);
    assert.match(yaml, /type: constant/);
});

test('per-configuration choices replace the implicit full arm and preserve every comparison', () => {
    const html = render({baselines: [], configurations: [
        {...defaults.configurations[0], optimizationSelection: ['load-only','affinity-only']},
        {...defaults.configurations[1], optimizationSelection: ['full','direct-vllm']},
    ]});
    assert.match(html, /4 benchmark tasks/);
    assert.match(html, /16 measured combinations/);
    assert.match(html, /Load-only routing/);
    assert.match(html, /Affinity Policy Only/);
    assert.doesNotMatch(html, /Derived from Configuration A/);
    assert.ok(html.indexOf('Configuration B') < html.indexOf('Load-only routing'));
});
