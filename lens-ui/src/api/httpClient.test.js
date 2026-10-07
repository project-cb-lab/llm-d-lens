import assert from 'node:assert/strict';
import test from 'node:test';
import { requestJson, postJson } from './httpClient.js';

test('JSON transport preserves Headers, signal and request payload', async (t) => {
    const signal = new AbortController().signal;
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        assert.equal(url, '/test');
        assert.equal(options.signal, signal);
        assert.equal(options.headers.get('X-Test'), 'yes');
        assert.equal(options.headers.get('Accept'), 'application/json');
        assert.equal(options.headers.get('Content-Type'), 'application/json');
        assert.equal(options.method, 'POST');
        assert.deepEqual(JSON.parse(options.body), { value: 1 });
        return Response.json({ saved: true });
    });
    assert.deepEqual(await postJson('/test', { value: 1 }, { signal, headers: new Headers({ 'X-Test': 'yes' }) }), { saved: true });
});

test('empty and non-JSON responses use explicit fallback', async (t) => {
    for (const response of [new Response(null, { status: 204 }), new Response('not JSON')]) {
        t.mock.method(globalThis, 'fetch', async () => response);
        assert.deepEqual(await requestJson('/test', {}, { fallback: [] }), []);
    }
});

test('structured and null errors retain metadata without masking the HTTP failure', async (t) => {
    for (const detail of [null, { message: 'Unavailable', code: 'offline', retryable: true }]) {
        t.mock.method(globalThis, 'fetch', async () => Response.json({ title: 'Service failure', detail }, { status: 503 }));
        await assert.rejects(requestJson('/test'), (error) => {
            assert.equal(error.status, 503);
            assert.equal(error.message, detail?.message || 'Service failure');
            assert.equal(error.retryable, Boolean(detail));
            assert.deepEqual(error.details, detail);
            return true;
        });
    }
});

test('transport preserves abort failures', async (t) => {
    const failure = new DOMException('Aborted', 'AbortError');
    t.mock.method(globalThis, 'fetch', async () => { throw failure; });
    await assert.rejects(requestJson('/test'), error => error === failure);
});

test('strict JSON clients reject malformed success but retain domain HTTP errors', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => new Response('not JSON'));
    await assert.rejects(requestJson('/test', {}, {strictJson:true}), SyntaxError);
    t.mock.method(globalThis, 'fetch', async () => new Response('not JSON',{status:503}));
    await assert.rejects(requestJson('/test', {}, {strictJson:true,errorFactory:()=>new Error('Domain failure')}), /Domain failure/);
});
