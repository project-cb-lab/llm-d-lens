import assert from 'node:assert/strict';
import test from 'node:test';
import { waitForSoftwareDownloads } from './clusterBackend.js';

test('download polling reports intermediate and terminal states', async (t) => {
    const pending = {llmD:{state:'downloading'}};
    const ready = {llmD:{state:'ready'},llmDBenchmark:{state:'failed'}};
    let calls=0;
    t.mock.method(globalThis,'fetch',async url => { assert.match(url,/a%2Fb\/software-downloads$/); return Response.json(calls++ ? ready : pending); });
    const updates=[];
    assert.deepEqual(await waitForSoftwareDownloads('a/b',{intervalMs:0,onUpdate:s=>updates.push(s)}),ready);
    assert.deepEqual(updates,[pending,ready]);
});

test('download polling rejects timeout instead of retrying forever', async (t) => {
    t.mock.method(globalThis,'fetch',async () => Response.json({llmD:{state:'downloading'}}));
    await assert.rejects(waitForSoftwareDownloads('a',{timeoutMs:-1,intervalMs:0}),/Timed out/);
});

test('cluster lookup forwards caller cancellation', async (t) => {
    const { loadClusters } = await import('./clusterBackend.js');
    const controller = new AbortController();
    t.mock.method(globalThis, 'fetch', async (_url, options) => {
        assert.equal(options.signal, controller.signal);
        return Response.json({ items: [] });
    });
    assert.deepEqual(await loadClusters({ signal: controller.signal }), { items: [] });
});

test('software workflow starts once, reports progress and preserves unreturned paths', async t => {
    const { downloadClusterSoftware } = await import('./clusterBackend.js');
    const cluster = { id: 'a/b', llmDRepoPath: 'old-d', llmDBenchmarkRepoPath: 'old-b', name: 'cluster' };
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        calls.push({ url, options });
        return Response.json(options.method === 'POST' ? {} : { llmD: { state: 'ready', path: 'new-d' }, llmDBenchmark: { state: 'idle' } });
    });
    const updates = [];
    const result = await downloadClusterSoftware(cluster, { onUpdate: s => updates.push(s) });
    assert.deepEqual(result, { ...cluster, llmDRepoPath: 'new-d' });
    assert.equal(cluster.llmDRepoPath, 'old-d');
    assert.equal(calls.length, 2);
    assert.match(calls[0].url, /a%2Fb/);
    // The backend downloads the profile-pinned revisions; the body is empty.
    assert.deepEqual(JSON.parse(calls[0].options.body), {});
    assert.equal(updates.length, 1);
});

test('software workflow aggregates repository failures and stops on POST failure', async t => {
    const { downloadClusterSoftware } = await import('./clusterBackend.js');
    t.mock.method(globalThis, 'fetch', async (_url, options) => Response.json(options.method === 'POST' ? {} : {
        llmD: { state: 'failed', error: 'invalid ref' }, llmDBenchmark: { state: 'failed', error: 'unavailable' },
    }));
    await assert.rejects(downloadClusterSoftware({ id: 'a' }, {}), /llm-d download failed: invalid ref; llm-d-benchmark download failed: unavailable/);
    let calls = 0;
    t.mock.method(globalThis, 'fetch', async () => { calls++; return Response.json({ detail: 'Denied' }, { status: 403 }); });
    await assert.rejects(downloadClusterSoftware({ id: 'a' }, {}), /Denied/);
    assert.equal(calls, 1);
});
