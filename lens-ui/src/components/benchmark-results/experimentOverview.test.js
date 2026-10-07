import test from 'node:test';
import assert from 'node:assert/strict';
import { overviewCases, overviewSeries } from './experimentOverview.js';
test('overview exposes asymmetric PD topology before measurements exist', () => {
 const rows=overviewCases({workflow:{},cases:[{case:{id:'pd',status:'queued',deployment_configuration:{provider_ref:'pd-disaggregation',content:{model:{name:'Qwen'},prefill:{replicaCount:2,tensorParallelSize:4},decode:{replicaCount:3,tensorParallelSize:2},runtime:{image:'vllm:xpu'}}}}}]});
 assert.equal(rows[0].topology,'2P × TP4 / 3D × TP2');
 assert.equal(rows[0].gpus,14);
 assert.equal(rows[0].model,'Qwen');
 assert.equal(rows[0].hardware,'Not recorded');
});
test('series separate workload lengths and load units, keeping missing results null', () => {
 const runs=[{id:'a',caseId:'a',load:1,loadKind:'rate',isl:128,osl:64,metrics:{throughput_tps:10}},{id:'b',caseId:'b',load:2,loadKind:'rate',isl:128,osl:64,metrics:{}},{id:'c',caseId:'c',load:1,loadKind:'concurrency',isl:128,osl:64,metrics:{}},{id:'d',caseId:'d',load:1,loadKind:'rate',isl:256,osl:64,metrics:{}}];
 const groups=overviewSeries(runs);
 assert.equal(groups.length,3);
 assert.equal(groups[0].rows.length,2);
 assert.equal(groups[0].rows[0]['a:throughput'],10);
 assert.equal(groups[0].rows[1]['b:throughput'],null);
});

test('overview includes saved accelerator identity alongside embedded topology', () => {
 const [row]=overviewCases({cases:[{case:{id:'a',configuration:{accelerator:'xpu'},deployment_configuration:{content:{decode:{replicaCount:4,tensorParallelSize:1}}},resource_snapshot:{gpu_telemetry:{devices:[{memory_total_bytes:24*1024**3}]}}}}]});
 assert.equal(row.hardware,'xpu · 24 GiB/card');
 assert.equal(row.gpus,4);
});
