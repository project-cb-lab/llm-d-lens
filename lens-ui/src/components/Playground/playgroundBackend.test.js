import assert from 'node:assert/strict';
import test from 'node:test';
import { listClusters, openClusterSession, streamPlaygroundChat } from './playgroundBackend.js';

test('cluster adapter preserves lists and both session ID spellings', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [{ id: 'a' }] }));
    assert.deepEqual(await listClusters(), [{ id: 'a' }]);
    for (const key of ['sessionId', 'session_id']) {
        t.mock.method(globalThis, 'fetch', async url => {
            assert.equal(new URL(url, 'http://local').searchParams.get('clusterId'), 'a/b');
            return Response.json({ [key]: 'session' });
        });
        assert.equal(await openClusterSession('a/b'), 'session');
    }
    t.mock.method(globalThis, 'fetch', async () => Response.json({}));
    await assert.rejects(openClusterSession('a'), /did not return an active session/);
});

test('chat remains streaming and preserves event delivery and request signal', async (t) => {
    const signal = new AbortController().signal;
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        assert.equal(url, '/api/playground/chat');
        assert.equal(options.signal, signal);
        assert.deepEqual(JSON.parse(options.body), { deployment: 'one', messages: [], readOnly: true });
        return new Response('event: delta\ndata: {"text":"hello"}\n\nevent: done\ndata: {}\n\n');
    });
    const events = [];
    await streamPlaygroundChat({ deployment: 'one', messages: [], readOnly: true }, (...event) => events.push(event), signal);
    assert.deepEqual(events, [['delta', { text: 'hello' }], ['done', {}]]);
});
