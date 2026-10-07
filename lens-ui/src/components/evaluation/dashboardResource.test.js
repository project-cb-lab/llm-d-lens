import assert from 'node:assert/strict';
import test from 'node:test';
import { createDashboardResource } from './dashboardResource.js';

test('return navigation retains content while overlapping refreshes share one request', async () => {
    let resolve;
    let calls = 0;
    const resource = createDashboardResource(() => {
        calls += 1;
        return new Promise(done => { resolve = done; });
    });
    assert.equal(resource.getSnapshot(), null);
    const first = resource.refresh();
    assert.equal(resource.refresh(), first);
    await Promise.resolve();
    resolve(['previous task']);
    await first;
    const refresh = resource.refresh();
    assert.deepEqual(resource.getSnapshot(), ['previous task']);
    assert.equal(resource.refresh(), refresh);
    await Promise.resolve();
    assert.equal(calls, 2);
    resolve(['updated task']);
    await refresh;
    assert.deepEqual(resource.getSnapshot(), ['updated task']);
});

test('failed refresh preserves last successful content and can be retried', async () => {
    let fails = false;
    const resource = createDashboardResource(async () => {
        if (fails) throw new Error('offline');
        return [];
    });
    await resource.refresh();
    fails = true;
    await assert.rejects(resource.refresh(), /offline/);
    assert.deepEqual(resource.getSnapshot(), []);
    fails = false;
    await resource.refresh();
    assert.deepEqual(resource.getSnapshot(), []);
});
