import assert from 'node:assert/strict';
import test from 'node:test';
import { streamAgenticRecommendation, streamAgenticRefinement } from './agenticDeploymentBackend.js';

test('agentic recommendation stream preserves events across response chunks', async (t) => {
    const signal = new AbortController().signal;
    const encoder = new TextEncoder();
    const previousDocument = globalThis.document;
    globalThis.document = { cookie: 'prism_csrf=csrf-value-123' };
    t.after(() => { globalThis.document = previousDocument; });
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        assert.equal(url, '/api/agentic-deployments/stream');
        assert.equal(options.signal, signal);
        assert.equal(options.headers['X-Prism-CSRF'], 'csrf-value-123');
        assert.deepEqual(JSON.parse(options.body), { model: 'Qwen/test' });
        return new Response(new ReadableStream({
            start(controller) {
                controller.enqueue(encoder.encode('event: progress\ndata: {"phase":"ai","message":"Wait'));
                controller.enqueue(encoder.encode('ing"}\n\nevent: complete\ndata: {"id":"run-1"}\n\n'));
                controller.close();
            },
        }));
    });
    const events = [];

    await streamAgenticRecommendation(
        { model: 'Qwen/test' }, (...event) => events.push(event), signal,
    );

    assert.deepEqual(events, [
        ['progress', { phase: 'ai', message: 'Waiting' }],
        ['complete', { id: 'run-1' }],
    ]);
});

test('agentic recalculation streams through the run-specific refine endpoint', async (t) => {
    const encoder = new TextEncoder();
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        assert.equal(url, '/api/agentic-deployments/run%2F1/refine/stream');
        assert.deepEqual(JSON.parse(options.body), { planner_prompt: 'prefer latency' });
        return new Response(new ReadableStream({
            start(controller) {
                controller.enqueue(encoder.encode('event: progress\ndata: {"phase":"scoring","status":"running"}\n\n'));
                controller.enqueue(encoder.encode('event: complete\ndata: {"id":"run/1","status":"awaiting_approval"}\n\n'));
                controller.close();
            },
        }));
    });
    const events = [];

    await streamAgenticRefinement(
        'run/1', { planner_prompt: 'prefer latency' }, (...event) => events.push(event),
    );

    assert.deepEqual(events, [
        ['progress', { phase: 'scoring', status: 'running' }],
        ['complete', { id: 'run/1', status: 'awaiting_approval' }],
    ]);
});
