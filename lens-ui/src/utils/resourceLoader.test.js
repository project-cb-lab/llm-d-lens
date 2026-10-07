import assert from 'node:assert/strict';
import test from 'node:test';
import { createResourceLoader } from './resourceLoader.js';

test('workspace and editor share metadata requests; explicit refresh reloads them', async () => {
    let requests = 0;
    const load = createResourceLoader(async () => ({ revision: ++requests }));
    const [workspace, wizard] = await Promise.all([load(), load()]);
    assert.equal(workspace, wizard);
    assert.equal((await load()).revision, 1);
    assert.equal((await load({ refresh: true })).revision, 2);
});

test('a failed metadata request can be retried', async () => {
    let requests = 0;
    const load = createResourceLoader(async () => {
        if (++requests === 1) throw new Error('Network error');
        return 'catalog';
    });
    await assert.rejects(load(), /Network error/);
    assert.equal(await load(), 'catalog');
});

const deferred = () => {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
};

test('latest resource request aborts and ignores an older loader even if it ignores cancellation', async () => {
    const { createLatestResourceRequest } = await import('./resourceLoader.js');
    const request = createLatestResourceRequest();
    const old = deferred();
    let oldSignal;
    const values = [];
    const handlers = { onSuccess: value => values.push(value) };
    const first = request.run(signal => { oldSignal = signal; return old.promise; }, handlers);
    await request.run(async () => 'new filter', handlers);
    assert.equal(oldSignal.aborted, true);
    old.resolve('old filter');
    await first;
    assert.deepEqual(values, ['new filter']);
});

test('resource cancellation suppresses late failure and settlement; the next load can retry', async () => {
    const { createLatestResourceRequest } = await import('./resourceLoader.js');
    const request = createLatestResourceRequest();
    const old = deferred();
    const events = [];
    const handlers = { onSuccess: v => events.push(v), onError: e => events.push(e.message), onSettled: () => events.push('settled') };
    const first = request.run(() => old.promise, handlers);
    request.cancel();
    old.reject(new Error('late'));
    await first;
    assert.deepEqual(events, []);
    await request.run(async () => { throw new Error('offline'); }, handlers);
    await request.run(async () => 'retried', handlers);
    assert.deepEqual(events, ['offline', 'settled', 'retried', 'settled']);
    assert.equal(request.pending, false);
});

test('quiet resource refresh skips an in-flight request', async () => {
    const { createLatestResourceRequest } = await import('./resourceLoader.js');
    const request = createLatestResourceRequest();
    const pending = deferred();
    let calls = 0;
    const first = request.run(() => { calls++; return pending.promise; });
    await request.run(() => { calls++; }, {}, { skipIfPending: true });
    assert.equal(calls, 1);
    pending.resolve([]);
    await first;
});
