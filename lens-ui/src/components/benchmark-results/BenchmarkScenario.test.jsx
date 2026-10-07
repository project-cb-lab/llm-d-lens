import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import GuideResultExplorer from './GuideResultExplorer.jsx';
import { readResultMetric } from './resultExplorer.js';

test('normalized TPOT reads its own recorded distribution without substituting TPOT', () => {
    const run = { metrics: { latency_distributions: { ntpot: { p95_ms: 12 }, tpot: { p95_ms: 7 } } } };
    assert.equal(readResultMetric(run, 'ntpot', 'p95').value, 12);
    assert.equal(readResultMetric({ metrics: { latency_distributions: { tpot: { p95_ms: 7 } } } }, 'ntpot').value, null);
});

test('overview leads with saved scenario configuration and offers every scenario', () => {
    const details = { cases: ['candidate', 'reference'].map((id, i) => ({ case: {
        id, kind: i ? 'baseline' : 'candidate', name: id,
        configuration: { guide: 'optimized-baseline', model: `model-${id}`, decode: { replicaCount: 2, tensorParallelSize: 4 }, runtime: { image: 'vllm:saved' } },
        benchmark: { isl: 4000, osl: 1000 }, metrics: { throughput_tps: 120 },
        resource_snapshot: { accelerator: 'H100' },
    } })) };
    const html = renderToStaticMarkup(<GuideResultExplorer details={details} guideType="optimized-baseline"/>);
    assert.match(html, /aria-label="Scenario"/);
    assert.match(html, /value="candidate"/);
    assert.match(html, /value="reference"/);
    assert.match(html, /model-candidate/);
    assert.match(html, /H100/);
    assert.match(html, /vllm:saved/);
    assert.match(html, /Output Tokens\/sec vs TTFT/);
    assert.match(html, /aria-pressed="false"[^>]*>NTPOT<\/button>/);
    assert.ok(html.indexOf('Benchmark scenario') < html.indexOf('Result analysis views'));
    assert.doesNotMatch(html, />Performance frontier<|>Experiment map<\/button>/);
});
