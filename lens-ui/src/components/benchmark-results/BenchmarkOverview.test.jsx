import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import BenchmarkFrontier from './BenchmarkFrontier.jsx';
import ExperimentMap from './ExperimentMap.jsx';

const run = { id: 'r1', caseId: 'a', caseName: 'Config A', load: 4, loadKind: 'concurrency', isl: 128, osl: 64, configuration: {}, metrics: { throughput_tps: 120, latency_distributions: { ttft: { p50_ms: 11, p95_ms: 23 }, itl: { p95_ms: 4 }, tpot: { p95_ms: 7 } } } };

test('merged measurements preserve all focused latency summaries without throughput', () => {
    const latencyOnly = { ...run, metrics: { latency_distributions: run.metrics.latency_distributions } };
    const html = renderToStaticMarkup(<ExperimentMap runs={[latencyOnly]} selected={latencyOnly} details={{}} />);
    const summary = html.slice(html.indexOf('aria-label="Measurement latency percentiles"'));
    for (const value of [11, 23, 4, 7]) assert.match(summary, new RegExp(`>${value}<`));
    assert.match(summary, /—/);
    assert.doesNotMatch(renderToStaticMarkup(<BenchmarkFrontier runs={[latencyOnly]} selected={latencyOnly} onSelect={() => {}}/>), /Point latency summaries/);
});

test('merged measurements retain output throughput and independent latency values for every load kind', () => {
    const rate = { ...run, id: 'r2', loadKind: 'rate', metrics: { throughput_tps: 80, latency_distributions: { ttft: { p95_ms: 31 } } } };
    const html = renderToStaticMarkup(<ExperimentMap runs={[run, rate]} details={{}} />);
    for (const value of [120, 23, 80, 31]) assert.match(html, new RegExp(`>${value}<`));
    assert.match(html, /req\/s/);
    assert.match(html, /concurrent/);
});

test('measurement table renders throughput and latency changes against a matched baseline', () => {
    const candidate = {...run, id: 'candidate', caseId: 'b', metrics: {throughput_tps: 180, latency_distributions: {ttft: {p95_ms: 46}}}};
    const html = renderToStaticMarkup(<ExperimentMap runs={[run,candidate]} details={{}}/>);
    assert.match(html, /\+50%/);
    assert.match(html, /\+100%/);
    assert.match(html, /text-emerald-300/);
    assert.match(html, /text-rose-300/);
});
