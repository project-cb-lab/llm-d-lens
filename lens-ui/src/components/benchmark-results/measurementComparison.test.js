import test from 'node:test';
import assert from 'node:assert/strict';
import { measurementBaseline, percentChange } from './measurementComparison.js';
const base = {id:'a',caseId:'a',load:4,loadKind:'concurrency',isl:128,osl:64};
const current = {...base,id:'b',caseId:'b'};
test('comparison matches workload dimensions across configurations',()=>{
 assert.deepEqual(measurementBaseline([base,current],current,'a'),base);
 for(const change of [{load:8},{loadKind:'rate'},{isl:256},{osl:128}]) assert.equal(measurementBaseline([{...base,...change},current],current,'a'),null);
});
test('ambiguous repeats and unknown workload dimensions have no baseline',()=>{
 assert.equal(measurementBaseline([base,{...base,id:'repeat'},current],current,'a'),null);
 assert.equal(measurementBaseline([{...base,isl:null}],{...current,isl:null},'a'),null);
});
test('percent changes preserve direction and omit unavailable or zero denominators',()=>{
 assert.equal(percentChange(120,100),20);
 assert.equal(percentChange(75,100),-25);
 assert.equal(percentChange(0,100),-100);
 for(const [value,reference] of [[1,0],[null,2],[2,undefined],[Infinity,3]]) assert.equal(percentChange(value,reference),null);
});
