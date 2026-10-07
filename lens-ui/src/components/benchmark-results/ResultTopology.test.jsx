import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ResultTopology, { buildResultTopology, topologyNodeMetrics } from './ResultTopology.jsx';

const render = (run, guideType) => renderToStaticMarkup(<ResultTopology run={run} guideType={guideType} />);
const observed = { per_pod: [{ pod: 'prefill-a', role: 'prefill', waiting_requests: { mean: 4, max: 8 } }, { pod: 'decode-b', role: 'decode', output_token_rate_tps: { mean: 123.4 } }] };

test('PD topology retains actual observed pods and identifies logical relationships', () => {
    const html = render({ id: 'one', observability: observed }, 'pd-disaggregation');
    assert.match(html, /prefill-a/);
    assert.match(html, /decode-b/);
    assert.match(html, /Prefill/);
    assert.match(html, /Decode/);
    assert.match(html, /Logical relationships/);
    assert.match(html, /not a request trace/);
    assert.match(html, /4 requests/);
    assert.doesNotMatch(html, /100%|healthy|success rate/i);
});

test('stage selection keeps case-wide pod summaries explicitly separate', () => {
    const html = render({ stage: { concurrency: 4 }, caseObservability: observed, observability: {} }, 'optimized-baseline');
    assert.match(html, /Case-wide snapshot/);
    assert.match(html, /not restricted to the selected stage/);
    assert.match(html, /prefill-a/);
});

test('missing telemetry never creates fake pod names or metrics', () => {
    const html = render({}, 'precise-prefix-cache-routing');
    assert.match(html, /KV index/);
    assert.match(html, /Logical component/);
    assert.match(html, /No pod or endpoint evidence was saved/);
    assert.doesNotMatch(html, /pod-1|0 requests|100%/);
});

test('tiered hierarchy only adds lower tiers supported by configuration or telemetry', () => {
    const missing = render({}, 'tiered-prefix-cache');
    assert.doesNotMatch(missing, />CPU RAM<|>Filesystem</);
    const configured = render({ configuration: { guide_variant: 'native/cpu/base', cacheCpuGiB: 8 } }, 'tiered-prefix-cache');
    assert.match(configured, /CPU RAM/);
    assert.match(configured, /Configured/);
    assert.doesNotMatch(configured, />Filesystem</);
});

test('observed pods merge resource identity without inventing pod endpoint associations', () => {
    const graph = buildResultTopology({ observability: observed, resources: { pods: [{ name: 'prefill-a', node_name: 'worker-a' }] }, caseObservability: { per_endpoint: [{ endpoint: '10.0.0.1:8000', waiting_requests: { mean: 2 } }] } }, 'pd-disaggregation');
    assert.equal(graph.nodes.filter(node => node.label === 'prefill-a').length, 1);
    assert.equal(graph.nodes.find(node => node.label === 'prefill-a').resource.node_name, 'worker-a');
    assert.ok(graph.nodes.some(node => node.label === '10.0.0.1:8000'));
    assert.ok(graph.edges.every(edge => edge.kind === 'logical'));
});

test('precise index uses persisted router schema and keeps case scope visible', () => {
    const html = render({ stage: {}, caseObservability: { router: { index_lookups_per_second: { mean: 12 } } } }, 'precise-prefix-cache-routing');
    assert.match(html, /12 lookups\/s/);
    assert.match(html, /Case-wide snapshot/);
});

test('namespace monitoring, benchmark, fallback-role and resource-only pods have no serving or KV edges', () => {
    const run = {
        observability: { per_pod: [
            { pod: 'benchmark-job', role: 'model-server', cpu_usage_cores: { mean: 2 } },
            { pod: 'prometheus', role: 'model-server', memory_working_set_bytes: { mean: 100 } },
            { pod: 'router-a', role: 'epp', cpu_usage_cores: { mean: 1 } },
            { pod: 'prefill-a', role: 'prefill', waiting_requests: { mean: 3 } },
        ] },
        resources: { pods: [{ name: 'unknown-pod' }, { name: 'resource-only-prefill', role: 'prefill' }] },
    };
    for (const guide of ['optimized-baseline', 'precise-prefix-cache-routing', 'pd-disaggregation']) {
        const graph = buildResultTopology(run, guide);
        for (const name of ['benchmark-job', 'prometheus', 'unknown-pod', 'resource-only-prefill']) {
            const node = graph.nodes.find(item => item.label === name);
            assert.ok(node, 'Retain saved objects for inspection');
            assert.ok(!graph.edges.some(edge => edge.from === node.id || edge.to === node.id), `${name} must remain unconnected`);
        }
        assert.ok(!graph.edges.some(edge => edge.from === 'pod:router-a' && edge.to === 'index'), 'Router is not identified as a KV publisher');
        assert.ok(graph.edges.some(edge => edge.to === 'pod:prefill-a'), 'Positive observed role can participate in logical serving relations');
    }
    assert.match(render(run, 'precise-prefix-cache-routing'), /Unclassified pod/);
});

test('saved canvas provides flow metric modes, fit, zoom, fullscreen and pan without refresh', () => {
    const html = render({ observability: observed }, 'pd-disaggregation');
    for (const label of ['Requests', 'Tokens', 'Zoom in', 'Zoom out', 'Fit view', 'Reset view', 'Fullscreen canvas']) assert.match(html, new RegExp(label));
    assert.match(html, /Drag the canvas to pan/);
    assert.doesNotMatch(html, /Refresh data|Live polling/);
});

test('node metric modes use saved request or token values with no flow fabrication', () => {
    const node = { metrics: { request_rate_rps: { mean: 3 }, output_token_rate_tps: { mean: 40 }, inflight_token_load: { mean: 11 }, waiting_requests: { mean: 5 } }, source: 'observability.per_pod.real' };
    assert.deepEqual(topologyNodeMetrics(node, 'request').map(metric => metric.key), ['request_rate_rps', 'waiting_requests']);
    assert.deepEqual(topologyNodeMetrics(node, 'token').map(metric => metric.key), ['output_token_rate_tps', 'inflight_token_load']);
    assert.deepEqual(topologyNodeMetrics({ metrics: { request_rate_rps: { mean: 3 } } }, 'token'), []);
    assert.deepEqual(topologyNodeMetrics({ metrics: { output_token_rate_tps: { mean: null } } }, 'token'), []);
});
