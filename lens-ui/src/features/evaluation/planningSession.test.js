import assert from 'node:assert/strict';
import test from 'node:test';
import { createPlanningSession } from './planningSession.js';

const request = { source: { mode: 'official' }, model: 'Qwen/Qwen3-0.6B', clusterSessionId: 'a', replicas: 1, runtimeImage: 'image:v1', guideVariant: 'base' };
const result = { validation: { status: 'valid' }, plannedDeployment: { content: 'yaml' } };

test('preview followed by save plans an identical configuration once; concurrent calls share the request', async () => {
    let calls = 0;
    const plan = createPlanningSession(async () => { calls++; return result; });
    await Promise.all([plan(request), plan({ ...request })]);
    await plan(request);
    assert.equal(calls, 1);
});

test('changed model, topology, image, variant or cluster and expired previews require new planning', async () => {
    let calls = 0, now = 0;
    const plan = createPlanningSession(async () => { calls++; return result; }, { now: () => now });
    await plan(request);
    for (const change of [{ model: 'Qwen/Qwen3-8B' }, { replicas: 2 }, { runtimeImage: 'image:v2' }, { guideVariant: 'native/cpu/base' }, { clusterSessionId: 'b' }]) await plan({ ...request, ...change });
    assert.equal(calls, 6);
    now = 60_001;
    await plan(request);
    assert.equal(calls, 7);
});

test('failed and invalid plans are retryable, and mutable sources bypass reuse', async () => {
    let calls = 0;
    const plan = createPlanningSession(async () => {
        calls++;
        if (calls === 1) throw new Error('Temporary network failure');
        if (calls === 2) return { validation: { status: 'invalid' } };
        return result;
    });
    await assert.rejects(plan(request), /Temporary/);
    await plan(request);
    await plan(request);
    await plan(request);
    assert.equal(calls, 3);
    await plan({ ...request, source: { mode: 'local' } });
    await plan({ ...request, source: { mode: 'local' } });
    assert.equal(calls, 5);
});
