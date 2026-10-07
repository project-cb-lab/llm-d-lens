import assert from 'node:assert/strict';
import test from 'node:test';
import { createSubmissionGuard } from './submissionGuard.js';
import { errorMessage } from './errorMessage.js';

test('two submissions in the same turn run once and unlock after settling', async () => {
    const run = createSubmissionGuard();
    let release;
    let calls = 0;
    const first = run(() => { calls++; return new Promise(resolve => { release = resolve; }); });
    await run(() => { calls++; });
    assert.equal(calls, 1);
    release(); await first;
    await run(() => { calls++; });
    assert.equal(calls, 2);
});

test('destructive success stays locked; failure permits retry and reports error', async () => {
    const run = createSubmissionGuard();
    const failure = new Error('failure');
    const events = [];
    await run(() => { throw failure; }, { onError: e => events.push(e), onSettled: () => events.push('settled'), keepPendingOnSuccess: true });
    assert.deepEqual(events, [failure, 'settled']);
    let calls = 0;
    await run(() => { calls++; }, { keepPendingOnSuccess: true });
    await run(() => { calls++; });
    assert.equal(calls, 1);
});

test('presentation errors preserve details, message and fallback order', () => {
    assert.equal(errorMessage({ details: 'detail', message: 'message' }), 'detail');
    assert.equal(errorMessage({ details: {}, message: 'message' }), 'message');
    assert.equal(errorMessage(null, 'fallback'), 'fallback');
});

test('timestamp formatting preserves missing and invalid fallbacks', async () => {
    const { formatTimestamp } = await import('./formatTimestamp.js');
    assert.equal(formatTimestamp(null), '—');
    assert.equal(formatTimestamp('bad-date'), 'bad-date');
    assert.equal(formatTimestamp('bad-date', { invalid: () => 'invalid' }), 'invalid');
    const date = '2026-01-01T00:00:00Z';
    assert.equal(formatTimestamp(date), new Date(date).toLocaleString());
});
