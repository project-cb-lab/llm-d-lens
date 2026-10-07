import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ResourceExplorer from './ResourceExplorer.jsx';
import { resourceSeries } from './resourceSeries.js';
const window = { start: '2026-09-09T00:00:00Z', end: '2026-09-09T00:01:00Z' };
test('resource charts convert recorded bytes without manufacturing CPU or Pod time series', () => {
  const run = { observability: { window, series: [{ timestamp: window.start, gpu_framebuffer_used_bytes: 2 ** 30, network_receive_bytes_per_second: 0 }] } };
  const [point] = resourceSeries(run);
  assert.equal(point.gpu_framebuffer_used_gib, 1);
  assert.equal(point.network_receive_mib_s, 0);
  assert.equal(point.cpu_memory_usage_gib, undefined);
  assert.equal(run.observability.series[0].gpu_framebuffer_used_gib, undefined);
});
test('Pod lifecycle and full-case averages are explicitly historical', () => {
  const run = { stage: true, scopeLabel: 'Stage without telemetry', caseObservability: { window, per_pod: [{ pod: 'engine', cpu_usage_cores: { mean: 2, max: 4 } }] }, resources: { pods: [{ name: 'engine', phase: 'Running' }] } };
  const html = renderToStaticMarkup(<ResourceExplorer run={run} />);
  assert.match(html, /phase at capture: Running/);
  assert.match(html, /Full-case window averages/);
  assert.match(html, /CPU/);
  assert.doesNotMatch(html, /Device memory saved sample/);
});
test('empty monitoring has a collection reason, not synthetic curves', () => {
  const html = renderToStaticMarkup(<ResourceExplorer run={{ observability: { reason: 'Prometheus returned no samples' } }} compact />);
  assert.match(html, /Prometheus returned no samples/);
  assert.doesNotMatch(html, /recharts-line|recharts-area/);
});

test('saved resource timeline exposes sample drilldown for each collected chart', () => {
  const run = { scopeLabel: 'Case', observability: { window, series: [{ timestamp: window.start, gpu_utilization_percent: 50, gpu_framebuffer_used_bytes: 2 ** 30 }] } };
  const html = renderToStaticMarkup(<ResourceExplorer run={run} />);
  assert.match(html, /GPU activity/);
  assert.match(html, /Device memory/);
  assert.match(html, /Inspect saved sample/);
  assert.match(html, /Device memory saved sample/);
  // Recharts lays out its SVG legend in the browser; SSR verifies the sample controls.
});


test('resource metrics remain available without collection source copy', () => {
  const run = { observability: { window, metric_sources: {
    gpu_utilization_percent: 'Intel XPUM device telemetry (DRA allocation)',
    gpu_framebuffer_used_bytes: 'Intel XPUM device telemetry (DRA allocation)',
  }, per_pod: [{ pod: 'engine', gpu_framebuffer_used_bytes: { mean: 2 ** 30, max: 2 ** 30 } }] } };
  const html = renderToStaticMarkup(<ResourceExplorer run={run} />);
  assert.doesNotMatch(html, /Collection sources &amp; scope|Intel XPUM/);
  assert.doesNotMatch(html, /memory use DCGM/);
  assert.match(html, /Device memory/);
});


test('framebuffer bytes appear only in the converted GiB memory chart', () => {
  const run = {observability:{window,series:[{timestamp:window.start,gpu_framebuffer_used_bytes:2**30}]}};
  const html=renderToStaticMarkup(<ResourceExplorer run={run}/>);
  assert.match(html,/Device memory saved sample/);
  assert.doesNotMatch(html,/GPU activity &amp; KV occupancy saved sample/);
});

test('Pod ranking shows mean, peak and deviation from the Pod average', () => {
  const run = {observability: {per_pod: [{pod:'a',cpu_usage_cores:{mean:1,max:3}},{pod:'b',cpu_usage_cores:{mean:3,max:5}}]}};
  const html = renderToStaticMarkup(<ResourceExplorer run={run}/>);
  assert.match(html, /vs Pod average/);
  assert.match(html, /\+50%/);
  assert.match(html, /-50%/);
  assert.match(html, />5<\/td>/);
  assert.ok(html.indexOf('title="b"') < html.indexOf('title="a"'));
});
