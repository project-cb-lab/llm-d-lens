import test from 'node:test';
import assert from 'node:assert/strict';
import { frontierPoints } from './frontierData.js';
const run = {id:'r',caseId:'a',load:1,configuration:{decode:{replicaCount:2,tensorParallelSize:2}},metrics:{throughput_tps:80,input_throughput_tps:40,throughput_rps:2,latency_distributions:{ttft:{p50_ms:10,p99_ms:90},ntpot:{p50_ms:3}}}};
test('percentiles use their own measurements and never substitute P95',()=>{
 const data=frontierPoints([run],{latency:'ttft',stats:['p50','p95','p99'],output:'throughput'});
 assert.deepEqual(data.map(p=>[p.stat,p.x,p.y]),[['p50',10,80],['p99',90,80]]);
});
test('total throughput and per-chip normalization require complete recorded inputs',()=>{
 assert.equal(frontierPoints([run],{latency:'ttft',stats:['p50'],output:'total',perChip:true})[0].y,30);
 assert.equal(frontierPoints([{...run,configuration:{}}],{latency:'ttft',stats:['p50'],output:'total',perChip:true}).length,0);
 assert.equal(frontierPoints([{...run,metrics:{...run.metrics,input_throughput_tps:undefined}}],{latency:'ttft',stats:['p50'],output:'total'}).length,0);
});
test('latency cap and log scale filter actual measurements',()=>{
 assert.equal(frontierPoints([run],{latency:'ttft',stats:['p50','p99'],output:'requestRate',cap:20})[0].y,2);
 assert.equal(frontierPoints([run],{latency:'ttft',stats:['p50','p99'],output:'throughput',cap:20}).length,1);
 const zero={...run,metrics:{...run.metrics,latency_distributions:{ttft:{p50_ms:0}}}};
 assert.equal(frontierPoints([zero],{latency:'ttft',stats:['p50'],output:'throughput',logScale:true}).length,0);
});
test('per-chip counts both P/D allocations and pipeline parallelism',()=>{
 const pd={...run,configuration:{prefill:{replicaCount:1,tensorParallelSize:2},decode:{replicaCount:2,tensorParallelSize:2,pipelineParallelSize:2}}};
 assert.equal(frontierPoints([pd],{latency:'ttft',stats:['p50'],output:'throughput',perChip:true})[0].y,8);
 const missingPrefill={...pd,configuration:{...pd.configuration,prefill:{replicaCount:1}}};
 assert.equal(frontierPoints([missingPrefill],{latency:'ttft',stats:['p50'],output:'throughput',perChip:true}).length,0);
});
