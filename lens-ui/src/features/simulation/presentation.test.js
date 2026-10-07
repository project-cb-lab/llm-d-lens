import test from 'node:test';
import assert from 'node:assert/strict';
import { numberAt, formatDuration, taskMetrics, backendOptionValue } from './presentation.js';
import { requestJson } from './client.js';

test('zero measurements remain available and invalid aliases are skipped', () => {
    assert.equal(numberAt({ a: null, b: '', c: 'invalid', d: 0 }, ['a', 'b', 'c', 'd']), 0);
    assert.equal(numberAt({}, ['value']), null);
    assert.equal(backendOptionValue('ttft_slo', 0.5), '500 ms');
});
test('running duration begins at execution rather than queued startup', () => {
    assert.equal(formatDuration({ status: 'running', started_at: '2026-01-01' }, {}, 0), 'Preparing…');
    assert.equal(formatDuration({ status: 'running', execution_started_at: '2026-01-01T00:00:00Z' }, {}, Date.parse('2026-01-01T00:00:30Z')), '30 s');
    assert.equal(formatDuration({ status: 'completed' }, { duration_seconds: 0 }), '0 s');
});
test('completion fallback preserves error percentage and SLO units', () => {
    const metrics = taskMetrics({ live_summary: { completion_timeline: [{ completed_requests: 10, failed_requests: 2 }] }, simulation: { backend_options: { ttft_slo: 0.5 } } });
    assert.equal(metrics.errorRate, 20);
    assert.equal(metrics.ttftSloMs, 500);
    assert.equal(metrics.tpot, null);
});
test('Simulation transport retains field errors and abort identity', async t => {
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: [{ msg: 'Invalid trace' }, { message: 'Missing backend' }] }, { status: 422 }));
    await assert.rejects(requestJson('/api/v1/simulation/tasks'), /Invalid trace; Missing backend/);
    const aborted = new DOMException('cancelled', 'AbortError');
    t.mock.method(globalThis, 'fetch', async () => { throw aborted; });
    await assert.rejects(requestJson('/api/v1/simulation/tasks'), e => e === aborted);
});
