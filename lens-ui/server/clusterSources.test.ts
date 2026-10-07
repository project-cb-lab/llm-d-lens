import assert from 'node:assert/strict';
import test from 'node:test';
import { loadClusterSources } from './clusterSources.ts';

test('source paths come from the selected cluster API record on every request', async (t) => {
    let checkout = '/tmp/cluster-a-v1';
    t.mock.method(globalThis, 'fetch', async (url: URL) => {
        assert.equal(url.pathname, '/api/cluster/clusters');
        return Response.json({ items: [
            { id: 'a', llmDRepoPath: checkout, llmDRef: 'v1' },
            { id: 'b', llmDRepoPath: '/tmp/cluster-b', llmDRef: 'v2' },
        ] });
    });
    assert.equal((await loadClusterSources({ clusterId: 'a' })).llmDRepoPath, '/tmp/cluster-a-v1');
    assert.equal((await loadClusterSources({ clusterId: 'b' })).llmDRepoPath, '/tmp/cluster-b');
    checkout = '/tmp/cluster-a-v3';
    assert.equal((await loadClusterSources({ clusterId: 'a' })).llmDRepoPath, '/tmp/cluster-a-v3');
});

test('missing cluster or downloaded source never uses a global checkout', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [{ id: 'a' }] }));
    await assert.rejects(loadClusterSources({}), /Select a cluster/);
    await assert.rejects(loadClusterSources({ clusterId: 'missing' }), /not found/);
    await assert.rejects(loadClusterSources({ clusterId: 'a' }), /Software Versions/);
});

test('session resolves its cluster and rejects conflicting cluster selection', async (t) => {
    t.mock.method(globalThis, 'fetch', async (url: URL) => Response.json(
        url.pathname.startsWith('/api/cluster-overview/sessions/')
            ? { serverId: 'a' }
            : { items: [{ id: 'a', llmDRepoPath: '/tmp/cluster-a' }] },
    ));
    assert.equal((await loadClusterSources({ clusterSessionId: 'session' })).llmDRepoPath, '/tmp/cluster-a');
    await assert.rejects(loadClusterSources({ clusterSessionId: 'session', clusterId: 'b' }), /do not match/);
});

test('unavailable or failed cluster API does not reuse another checkout', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [{
        id: 'a', llmDRepoPath: '/tmp/stale', deploymentSource: { resolved_from: 'unavailable' },
    }] }));
    await assert.rejects(loadClusterSources({ clusterId: 'a' }), /Software Versions/);
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'offline' }, { status: 503 }));
    await assert.rejects(loadClusterSources({ clusterId: 'a' }), /offline/);
});
