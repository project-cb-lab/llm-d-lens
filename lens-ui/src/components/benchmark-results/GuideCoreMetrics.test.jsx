import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToStaticMarkup as render} from 'react-dom/server';
import GuideCoreMetrics from './GuideCoreMetrics.jsx';

const point=(id,load,extra={})=>({id,caseId:'candidate',kind:'candidate',caseName:'Candidate',guide:'optimized-baseline',loadKind:'concurrency',load,isl:1024,osl:128,metrics:{throughput_tps:load*100,latency_distributions:{ttft:{p95_ms:load*10}}},...extra});
const markup=(runs,selected=runs[0])=>render(<GuideCoreMetrics runs={runs} selected={selected} guideType="optimized-baseline"/>);

test('all concurrency results are visible together without result or reference dropdowns',()=>{
 const html=markup([point('r8',8),point('r1',1),point('r4',4)]);
 assert.match(html,/Latency, throughput &amp; load balance/);
 assert.doesNotMatch(html,/Core metrics|<select/);
 for(const value of [100,400,800])assert.match(html,new RegExp(`>${value}<`));
 assert.ok(html.indexOf('Inspect load point r1') < html.indexOf('Inspect load point r4'));
 assert.ok(html.indexOf('Inspect load point r4') < html.indexOf('Inspect load point r8'));
 assert.match(html,/aria-label="Latency percentile"/);
});

test('different workload lengths and load kinds stay in separate comparison tables',()=>{
 const html=markup([point('a',1),point('b',1,{isl:2048}),point('c',1,{loadKind:'rate'})]);
 assert.equal((html.match(/<table /g)||[]).length,3);
 assert.match(html,/Concurrency/);
 assert.match(html,/Request rate/);
 assert.match(html,/1,024 \/ 128/);
 assert.match(html,/2,048 \/ 128/);
});

test('full-case telemetry stays accessible without joining the stage comparison',()=>{
 const stage=point('stage',1,{stage:true});
 const full=point('candidate:case-summary',null,{metrics:{throughput_tps:9999},scopeLabel:'Case · full run'});
 const html=markup([stage,full]);
 assert.match(html,/View full-run telemetry candidate/);
 assert.doesNotMatch(html,/>9,999</);
 assert.match(markup([stage,full],full),/>9,999 /);
});

test('comparison uses a unique baseline and preserves repeats without arbitrary reference selection',()=>{
 const candidate=point('c',2);
 const baseline=point('b',2,{caseId:'baseline',kind:'baseline',metrics:{throughput_tps:100}});
 assert.match(markup([candidate,baseline]),/\+100%/);
 assert.doesNotMatch(markup([candidate,baseline,{...baseline,id:'repeat'}]),/\+100%/);
});

test('repeat rows are distinguishable and multiple matching baseline observations remain ambiguous',()=>{
 const candidate=point('c',2);
 const baseline=point('b',2,{caseId:'baseline',kind:'baseline',metrics:{throughput_tps:100}});
 const html=markup([candidate,baseline,{...baseline,id:'repeat'},{...baseline,id:'other-baseline',caseId:'other-baseline'}]);
 assert.match(html,/Observation 1/);
 assert.match(html,/Observation 2/);
 assert.doesNotMatch(html,/\+100%/);
});


test('saved request metrics remain available in selected result details',()=>{
 const run=point('saved',4,{metrics:{request_count:42,success_count:40,failure_count:2,latency_distributions:{itl:{p95_ms:17},tpot:{p95_ms:29}}}});
 const html=markup([run]);
 assert.match(html,/Request results/);
 for(const value of [42,40,2,17,29])assert.match(html,new RegExp(`>${value} `));
 assert.doesNotMatch(html,/aria-label="Metric display"/);
 assert.doesNotMatch(html,/>Chart</);
});


test('stage without telemetry explains full-run collection failure without using full-run values',()=>{
 const run=point('stage',4,{stage:true,caseObservability:{status:'empty',reason:'No samples in saved window',summary:{queue_depth:{mean:999}}}});
 const html=markup([run]);
 assert.match(html,/Full-run telemetry: No samples in saved window/);
 assert.doesNotMatch(html,/>999</);
});

test('request details expose strict goodput with recorded evidence',()=>{
 const run=point('strict',1);
 run.metrics.request_slo_goodput={schema_version:1,status:'measured',value:1.25,duration_seconds:60};
 const html=markup([run]);
 assert.match(html,/Per-request SLO goodput/);
 assert.match(html,/1.25/);
});
