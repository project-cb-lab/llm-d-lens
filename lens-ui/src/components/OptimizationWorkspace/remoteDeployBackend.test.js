import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import test from 'node:test';
import { listDeploymentExecutions, getDeploymentExecutions, stopLocalDeploymentCase, deleteDeploymentExecution, testRemoteTarget } from './remoteDeployBackend.js';

test('deployment list preserves repeated status filters and response validation', async (t) => {
    const signal = new AbortController().signal;
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        const query = new URL(url, 'http://localhost').searchParams;
        assert.deepEqual(query.getAll('statuses'), ['ready', 'failed']);
        assert.equal(options.signal, signal);
        return Response.json({ items: 'invalid' });
    });
    await assert.rejects(listDeploymentExecutions({ statuses: ['ready', 'failed'], signal }), /list response is invalid/);
});

test('endpoint reconnection failure retains ready deployment', async (t) => {
    t.mock.method(globalThis, 'fetch', async url => url.includes('/endpoint')
        ? Response.json({ detail: 'offline' }, { status: 503 })
        : Response.json({ items: [{ execution_id: 'one', endpoint: 'http://service' }] }));
    assert.deepEqual(await getDeploymentExecutions({ status: 'ready' }), [{ execution_id: 'one', endpoint: 'http://service', endpoint_health: 'unknown' }]);
});

test('stop binds session before action; remote credentials stay in per-request body', async (t) => {
    const original = globalThis.sessionStorage;
    globalThis.sessionStorage = { getItem: () => 'session-1' };
    t.after(() => { if (original === undefined) delete globalThis.sessionStorage; else globalThis.sessionStorage = original; });
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push([url, JSON.parse(options.body)]); return Response.json({}); });
    await stopLocalDeploymentCase('run/a', 'case/b');
    assert.match(calls[0][0], /run%2Fa\/cluster-session$/);
    assert.deepEqual(calls[0][1], { cluster_session_id: 'session-1' });
    assert.match(calls[1][0], /case%2Fb\/stop$/);
    const password = randomUUID();
    await testRemoteTarget({ authMethod: 'password' }, password);
    assert.deepEqual(calls[2][1].credentials, { password });
    await testRemoteTarget({ authMethod: 'key' }, 'unused');
    assert.deepEqual(calls[3][1].credentials, {});
});

test('delete retains strict 204 contract and structured errors', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }));
    assert.equal(await deleteDeploymentExecution('one'), undefined);
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: { message: 'Busy', code: 'busy' } }, { status: 409 }));
    await assert.rejects(deleteDeploymentExecution('one'), error => error.status === 409 && error.code === 'busy');
    t.mock.method(globalThis, 'fetch', async () => Response.json({}));
    await assert.rejects(deleteDeploymentExecution('one'), error => error.status === 200);
});

test('delete rebinds the cluster session first when a run id is given', async (t) => {
    const original = globalThis.sessionStorage;
    globalThis.sessionStorage = { getItem: () => 'session-1' };
    t.after(() => { if (original === undefined) delete globalThis.sessionStorage; else globalThis.sessionStorage = original; });
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url) => {
        calls.push(url);
        return url.includes('/cluster-session') ? Response.json({}) : new Response(null, { status: 204 });
    });
    await deleteDeploymentExecution('one', { runId: 'run/a' });
    assert.match(calls[0], /run%2Fa\/cluster-session$/);
    assert.match(calls[1], /executions\/one$/);
});

test('delete without a run id skips rebinding, matching existing no-arg callers', async (t) => {
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url) => { calls.push(url); return new Response(null, { status: 204 }); });
    await deleteDeploymentExecution('one');
    assert.equal(calls.length, 1);
    assert.match(calls[0], /executions\/one$/);
});

test('pending deployment run lookup forwards caller cancellation', async (t) => {
    const { searchLocalDeploymentRuns } = await import('./remoteDeployBackend.js');
    const controller = new AbortController();
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        assert.match(url, /query=a%20b/);
        assert.equal(options.signal, controller.signal);
        return Response.json([]);
    });
    assert.deepEqual(await searchLocalDeploymentRuns('a b', { signal: controller.signal }), []);
});
