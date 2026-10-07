import test from 'node:test';
import assert from 'node:assert/strict';
import { historicalFlow, historicalFlows } from './historicalFlow.js';
const observation={window:{start:'2026-09-11T00:00:00Z',end:'2026-09-11T00:01:00Z'},namespace:'removed',per_pod:[{pod:'worker',role:'model-server',request_rate_rps:{mean:99}},{pod:'decode-0',role:'decode',request_rate_rps:{mean:0},kv_cache_usage_percent:{mean:20}}],router:{request_rate_rps:{mean:2}},summary:{epp_inflight_requests:{mean:3}}};
test('historical flow preserves recorded means and zero without attributing aggregate traffic to pods',()=>{
 const data=historicalFlow({caseObservability:observation});
 assert.equal(data.components.length,2);
 assert.equal(data.components[0].instances[0].request_rate,2);
 assert.equal(data.components[1].instances.length,1);
 assert.equal(data.components[1].instances[0].request_rate,0);
 assert.equal(data.components[1].instances[0].output_token_rate,null);
 assert.equal(data.components[1].instances[0].kv_cache_usage_perc,20);
});
test('cleaned deployments retain history once per case even with multiple stages',()=>{
 const details={cases:[{case:{id:'done',status:'succeeded',metrics:{observability:observation},rate_stage_results:[{rate:1},{rate:2}]},deployment_cases:[{status:'cleaned'}]}]};
 assert.equal(historicalFlows(details).length,1);
 assert.equal(historicalFlows(details)[0].run.caseId,'done');
});
test('missing history is not reconstructed from current deployment snapshots',()=>{
 assert.equal(historicalFlow({caseObservability:{},resources:{pods:[{role:'decode'}]}}),null);
 assert.deepEqual(historicalFlows(null),[]);
});
