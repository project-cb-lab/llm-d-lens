import test from 'node:test';
import assert from 'node:assert/strict';
import { experimentPoints, experimentHierarchy, comparisonLines, matchingRuns } from './linkedExperiment.js';
const runs = ['a','b'].flatMap(caseId => [1,2,4,8,16,32,64,128].flatMap(load => [512,4096].map(isl => ({id:`${caseId}:${load}:${isl}`,caseId,load,loadKind:'concurrency',isl,osl:64,stage:true,metrics:{throughput_tps:load*10}}))));
test('2 configurations × 8 loads × 2 lengths retain 32 independent selectable points',()=>{
 const all=[...runs,{id:'a:summary',caseId:'a',load:null,stage:false}];
 assert.equal(experimentPoints(all).length,32);
 const graph=experimentHierarchy(all);
 assert.equal(graph.length,2);assert.equal(graph[0].loads.length,8);assert.equal(graph[0].loads[0].runs.length,2);
 assert.equal(new Set(graph.flatMap(c=>c.loads.flatMap(l=>l.runs.map(r=>r.id)))).size,32);
});
test('input-length comparison produces all 16 condition lines; concurrency view produces 4',()=>{
 const lines=comparisonLines(runs,'isl','throughput','p95');
 assert.equal(lines.length,16);assert.ok(lines.every(l=>l.points.length===2));
 assert.equal(comparisonLines(runs,'load','throughput','p95').length,4);
});
test('repeated observations do not overwrite each other, missing values stay null',()=>{
 const lines=comparisonLines([...runs,{...runs[0],id:'repeat',metrics:{}}],'load','throughput','p95');
 assert.equal(lines.reduce((n,l)=>n+l.points.length,0),33);
 assert.ok(lines.flatMap(l=>l.points).some(p=>p.run.id==='repeat' && p.y===null));
});
test('linked comparisons match load kind, load, input and output length across cases',()=>{
 assert.equal(matchingRuns(runs,runs[0]).length,2);
 assert.equal(matchingRuns([...runs,{...runs[0],id:'rate',loadKind:'rate'}],runs[0]).length,2);
});
test('equal-topology optimization arms have distinct human-readable chart labels', async()=>{
 const {shortCase}=await import('./linkedExperiment.js');
 const base={configuration:{model:{name:'Qwen/Test'},decode:{replicaCount:2,tensorParallelSize:1}},kind:'baseline',guide:'optimized-baseline'};
 const labels=['affinity-only','load-only','router-neutral'].map(baselineType=>shortCase({...base,baselineType}));
 assert.equal(new Set(labels).size,3);
 assert.notEqual(shortCase({...base,kind:'guide',guide:'pd-disaggregation'}),shortCase({...base,kind:'guide',guide:'tiered-prefix-cache'}));
});
