import assert from 'node:assert/strict';
import test from 'node:test';
import { loadGuideCatalog, planGuideDeployment, prepareGuideSource } from './guidePlanningBackend.js';

test('guide catalogs are isolated by selected cluster', async (t) => {
    t.mock.method(globalThis, 'fetch', async (url) => {
        const clusterId = new URL(url, 'http://localhost').searchParams.get('clusterId');
        return Response.json({ guides: [{ id: `guide-for-${clusterId}` }] });
    });
    const [a, b] = await Promise.all([loadGuideCatalog({ clusterId: 'a' }), loadGuideCatalog({ clusterId: 'b' })]);
    assert.equal(a.guides[0].id, 'guide-for-a');
    assert.equal(b.guides[0].id, 'guide-for-b');
    assert.equal((await loadGuideCatalog({ clusterId: 'a' })).guides[0].id, 'guide-for-a');
});

test('planning and preparation preserve explicit cluster or session context', async (t) => {
    t.mock.method(globalThis, 'fetch', async (_url, options) => Response.json(JSON.parse(options.body)));
    assert.equal((await prepareGuideSource({ clusterId: 'b' })).clusterId, 'b');
    const sessionPlan = await planGuideDeployment({ clusterSessionId: 'session-a' });
    assert.equal(sessionPlan.clusterSessionId, 'session-a');
    assert.equal(sessionPlan.clusterId, undefined);
});
