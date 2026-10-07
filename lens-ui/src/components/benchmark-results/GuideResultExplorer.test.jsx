import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import GuideResultExplorer from './GuideResultExplorer.jsx';

const fixture = (guide) => ({ workflow: { id: 'run-1', status: 'succeeded', name: 'Measured experiment' }, cases: [{ case: {
  id: 'candidate', kind: 'candidate', status: 'succeeded', configuration: { guide, model: 'test-model' },
  benchmark: { parallelism: 4 }, metrics: { throughput_tps: 120, success_rate: 100,
    latency_distributions: { ttft: { p95_ms: 23 }, itl: { p95_ms: 4 }, tpot: { p95_ms: 7 } },
    slo_goodput_rps: 999, capacity_analysis: { slo_goodput_rps: 999 },
  },
} }] });

test('common first screen keeps ITL and TPOT distinct and does not call capacity goodput measured', () => {
  const html = renderToStaticMarkup(<GuideResultExplorer details={fixture('pd-disaggregation')} guideType="pd-disaggregation" />);
  assert.match(html, /TTFT/); assert.match(html, /ITL/); assert.match(html, /TPOT/);
  assert.match(html, /120/); assert.match(html, /23/);
  assert.doesNotMatch(html, />999</);
  assert.match(html, /Not collected|Unavailable|unavailable|not recorded/i);
});

test('four Guides have their own mechanism question and missing evidence without fabricated values', () => {
  const results = ['optimized-baseline', 'precise-prefix-cache-routing', 'tiered-prefix-cache', 'pd-disaggregation'].map(guide =>
    renderToStaticMarkup(<GuideResultExplorer details={fixture(guide)} guideType={guide} />));
  assert.equal(new Set(results).size, 4);
  for (const html of results) {
    assert.doesNotMatch(html.match(/<nav[\s\S]*?<\/nav>/)?.[0] || '', />Metric sources<|>Topology<|>Configuration<|>Diagnosis</);
    assert.doesNotMatch(html, /NaN|Infinity|STRONG.*BENEFIT|100%.*verified/);
  }
});

test('empty result retains collection limitations instead of rendering a synthetic dashboard', () => {
  const html = renderToStaticMarkup(<GuideResultExplorer details={{workflow:{status:'running'},cases:[]}} guideType="tiered-prefix-cache" />);
  assert.match(html, /No measured results/);
});

test('selected candidate Guide overrides workflow fallback profile', () => {
  const html = renderToStaticMarkup(<GuideResultExplorer details={fixture('pd-disaggregation')} guideType="optimized-baseline" />);
  assert.match(html, /Prefill \/ Decode balance/);
  assert.doesNotMatch(html, /Cache affinity &amp; load balance/);
});

test('overview keeps unmet targets visible and hides advanced chart controls', () => {
  const details = fixture('optimized-baseline');
  details.cases[0].case.metrics.success_rate = 64;
  details.cases[0].case.sla_targets = { success_rate_min_percent: 99 };
  const html = renderToStaticMarkup(<GuideResultExplorer details={details} guideType="optimized-baseline" />);
  assert.doesNotMatch(html, /Benchmark overview/);
  assert.doesNotMatch(html, /Selected benchmark result|Chart settings/);
  assert.doesNotMatch(html, /Tradeoff workload lengths|Resources during benchmark|Historical time range/);
  assert.match(html, /Not met/);
  assert.match(html, /Throughput|throughput/);
  assert.doesNotMatch(html, /Guide result workspace|Experiment arm/);
});

test('parent-controlled sections show one analysis surface without nested navigation or duplicate scenario',()=>{
 for(const view of ['resources']) {
  const html=renderToStaticMarkup(<GuideResultExplorer details={fixture('optimized-baseline')} guideType="optimized-baseline" view={view} onViewChange={()=>{}}/>);
  assert.doesNotMatch(html,/aria-label="Result analysis views"|aria-label="Benchmark scenario"|Selected experiment point/);
  if(view==='mechanism')assert.doesNotMatch(html,/aria-label="Selected configuration"/);
  else assert.match(html,/aria-label="Selected configuration"/);
 }
});

test('guide priorities render measured values before general charts', () => {
  const expected = {
    'optimized-baseline': ['Token-load CV', 'Waiting requests'],
    'pd-disaggregation': ['Prefill queue', 'Decode queue', 'KV transfer latency'],
    'precise-prefix-cache-routing': ['Actual engine prompt reuse', 'Engine local computation', 'Cached-token recomputation'],
    'tiered-prefix-cache': ['KV restore', 'HBM KV usage', 'Distinct reusable KV working set'],
  };
  for (const [guide, labels] of Object.entries(expected)) {
    const details = fixture(guide);
    details.cases[0].case.metrics.observability = {
      window: {start:'2026-09-09T00:00:00Z',end:'2026-09-09T00:01:00Z'},
      summary: {kv_restore_bytes_per_second:{mean:4096},queue_depth:{mean:3}},
      derived: {role_summary:{prefill:{waiting_requests:2},decode:{waiting_requests:1}}},
    };
    const html = renderToStaticMarkup(<GuideResultExplorer details={details} guideType={guide}/>);
    const start = html.indexOf('aria-label="Guide core metrics"');
    assert.ok(start >= 0, `${guide} needs a core metric section`);
    assert.doesNotMatch(html, /aria-label="Linked comparison"/);
    const focus = html.slice(start, html.indexOf('aria-label="Linked comparison"'));
    for (const label of labels) assert.ok(focus.includes(label), `${guide}: ${label}`);
    assert.match(focus, /Not collected/);
  }
});

test('full-run telemetry action offers full-case evidence without filling missing stage metrics',()=>{
 const details=fixture('precise-prefix-cache-routing');
 details.cases[0].case.metrics.observability={window:{start:'2026-09-09T00:00:00Z',end:'2026-09-09T00:01:00Z'},summary:{engine_prompt_local_compute_tps:{mean:9999}}};
 details.cases[0].case.rate_stage_results=[{rate:10,metrics:{throughput_tps:120}}];
 const html=renderToStaticMarkup(<GuideResultExplorer details={details} guideType="precise-prefix-cache-routing"/>);
 assert.match(html,/View full-run telemetry candidate/);
 assert.match(html,/Full-run evidence/);
 assert.doesNotMatch(html,/>9,999</);
});


test('Compare has public metrics across Guides and keeps the overview separate', () => {
 const details=fixture('optimized-baseline');
 const other=fixture('pd-disaggregation').cases[0];
 other.case.id='pd';
 details.cases.push(other);
 for (const entry of details.cases) entry.case.rate_stage_results=[{rate:2,metrics:entry.case.metrics}];
 const html=renderToStaticMarkup(<GuideResultExplorer details={details} view="compare"/>);
 assert.match(html,/aria-label="Linked comparison"/);
 assert.match(html,/aria-label="Comparison metric"/);
 assert.doesNotMatch(html,/aria-label="Guide core metrics"|aria-label="Resource controls"/);
 assert.match(html,/EPP full/);
 assert.match(html,/P\/D/);
});
