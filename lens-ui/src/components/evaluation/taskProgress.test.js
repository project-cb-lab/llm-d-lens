import test from 'node:test';
import assert from 'node:assert/strict';
import { taskProgress } from './taskProgress.js';

test('current case drives stages instead of completed earlier cases', () => {
    const progress = taskProgress({kind:'workflow',status:'running',active_case_id:'b',cases:[{id:'a',status:'succeeded'},{id:'b',status:'deploying'}]});
    assert.deepEqual(progress.segments.map(s=>s.state), ['complete','active','pending','pending']);
    assert.equal(progress.currentLabel,'Case 2');
});
test('failed stage remains red with later stages pending', () => {
    const progress = taskProgress({kind:'workflow',status:'failed',cases:[{status:'failed',failed_stage:'deploying',error:'NotFound'}]});
    assert.deepEqual(progress.segments.map(s=>s.state), ['complete','failed','pending','pending']);
    assert.equal(progress.message,'Failed at: Deploy');
    assert.equal(progress.error,'NotFound');
    assert.equal(progress.active,false);
});
test('historical benchmark failures use saved run links, missing evidence is unknown', () => {
    assert.equal(taskProgress({kind:'workflow',status:'failed',cases:[{status:'failed',evaluation_run_id:'run'}]}).message,'Failed at: Benchmark');
    assert.ok(taskProgress({kind:'workflow',status:'failed'}).segments.every(s=>s.state==='unknown'));
});
test('success completes every segment, endpoint tasks omit deployment', () => {
    assert.ok(taskProgress({kind:'workflow',status:'succeeded'}).segments.every(s=>s.state==='complete'));
    assert.deepEqual(taskProgress({kind:'benchmark',status:'running'}).segments.map(s=>s.id),['queued','benchmarking','complete']);
});
