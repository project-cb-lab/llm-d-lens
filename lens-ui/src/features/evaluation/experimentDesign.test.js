import test from 'node:test';
import assert from 'node:assert/strict';
import { optimizationOptions, selectedOptimizationPlan, recommendationBudget } from './experimentDesign.js';
test('each configuration independently selects full or ablated routing', () => {
 const options = optimizationOptions('optimized-baseline');
 assert.deepEqual(options.map(x=>x.id), ['full','router-neutral','affinity-only','load-only','direct-vllm']);
 assert.deepEqual(selectedOptimizationPlan('optimized-baseline',['affinity-only','load-only']), {include_configuration:false, include_baseline:true, baseline_types:['affinity-only','load-only']});
 assert.equal(selectedOptimizationPlan('optimized-baseline',['full']).include_baseline,false);
 assert.throws(()=>selectedOptimizationPlan('optimized-baseline',[]), /Select/);
 assert.deepEqual(selectedOptimizationPlan('pd-disaggregation',['load-only']).baseline_types,['load-only']);
});
test('available zero remains zero while total recommendations use all physical cards', () => {
 const hardware={gpuCount:8, usableGpuCount:0, availableGpuCount:2, configuredGpuLimit:4};
 assert.equal(recommendationBudget(hardware,'available'),0);
 assert.equal(recommendationBudget(hardware,'total'),8);
 assert.equal(recommendationBudget(null,'total'),0);
});

test('component composition compiles independent routing policies and validates dependencies', async()=>{
 const {compileOptimizationComponents}=await import('./experimentDesign.js');
 assert.equal(compileOptimizationComponents('optimized-baseline',{epp:true,prefix:true,load:false}),'affinity-only');
 assert.equal(compileOptimizationComponents('optimized-baseline',{epp:true,prefix:false,load:true}),'load-only');
 assert.equal(compileOptimizationComponents('optimized-baseline',{epp:true,prefix:false,load:false}),'router-neutral');
 assert.equal(compileOptimizationComponents('optimized-baseline',{epp:false,prefix:false,load:false}),'direct-vllm');
 assert.throws(()=>compileOptimizationComponents('optimized-baseline',{epp:false,prefix:true}),/EPP/);
 assert.equal(compileOptimizationComponents('pd-disaggregation',{epp:true,prefix:true,load:true,mechanism:false}),'optimized-baseline');
 assert.equal(compileOptimizationComponents('pd-disaggregation',{epp:true,prefix:true,load:true,mechanism:true}),'full');
 assert.throws(()=>compileOptimizationComponents('pd-disaggregation',{epp:true,prefix:false,load:true,mechanism:true}),/requires/);
});

test('generation confirmation replaces a previous resource budget in the outgoing request',async()=>{
 const {withRecommendationBudget}=await import('./experimentDesign.js');
 const current={resourceBasis:'available',recommendationGpuCount:0,replicas:4,tensorParallelSize:2};
 const next=withRecommendationBudget(current,{availableGpuCount:0,gpuCount:8},'total');
 assert.equal(next.resourceBasis,'total');assert.equal(next.recommendationGpuCount,8);
 assert.equal(next.replicas,4);assert.equal(next.tensorParallelSize,2);
 assert.equal(current.recommendationGpuCount,0);
});
