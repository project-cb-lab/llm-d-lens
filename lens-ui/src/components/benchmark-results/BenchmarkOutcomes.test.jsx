import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import BenchmarkOutcomes from './BenchmarkOutcomes.jsx';
const makeRun = (id, throughput, ttft) => ({id,caseId:id,stage:true,load:8,loadKind:'concurrency',isl:512,osl:64,configuration:{model:id},metrics:{throughput_tps:throughput,latency_distributions:{ttft:{p95_ms:ttft}}}});
test('outcomes compare recorded values at matching conditions and keep missing metrics unavailable',()=>{
 const selected=makeRun('selected',150,60),reference=makeRun('reference',100,100);
 const html=renderToStaticMarkup(<BenchmarkOutcomes runs={[selected,reference]} selected={selected}/>);
 assert.match(html,/\+50%/);assert.match(html,/-40%/);assert.match(html,/Change unavailable/);
 assert.doesNotMatch(html,/NaN|Infinity/);
});
test('different or unknown workloads cannot produce a percentage comparison',()=>{
 const selected=makeRun('selected',150,60),reference={...makeRun('reference',100,100),isl:4096};
 const unknown=[selected,reference].map(run=>({...run,isl:null,load:null}));
 for(const runs of [[selected,reference],unknown]) {
  const html=renderToStaticMarkup(<BenchmarkOutcomes runs={runs} selected={runs[0]}/>);
  assert.match(html,/>150 /);assert.match(html,/>60 /);
  assert.doesNotMatch(html,/%|Outcome reference|Change unavailable/);
 }
 assert.match(renderToStaticMarkup(<BenchmarkOutcomes runs={unknown} selected={unknown[0]}/>),/Full run/);
});
test('a single result displays absolute metrics, preserves zero and marks missing P95',()=>{
 const selected=makeRun('selected',0,60);
 selected.metrics.latency_distributions.tpot={p50_ms:12};
 const html=renderToStaticMarkup(<BenchmarkOutcomes runs={[selected]} selected={selected}/>);
 for(const label of ['Output throughput','Time to first token · P95','Time per output token · P95']) assert.ok(html.includes(label));
 assert.match(html,/>0 /);assert.match(html,/>60 /);assert.match(html,/Not recorded/);
 assert.doesNotMatch(html,/>12 |Outcome reference|Change unavailable|No reference result/);
});
test('a zero reference does not produce infinite percentage gain',()=>{
 const selected=makeRun('selected',150,60),reference=makeRun('reference',0,0);
 const html=renderToStaticMarkup(<BenchmarkOutcomes runs={[selected,reference]} selected={selected}/>);
 assert.match(html,/Change unavailable/);assert.doesNotMatch(html,/Infinity|NaN/);
});
