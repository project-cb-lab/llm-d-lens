import assert from 'node:assert/strict';
import test from 'node:test';
import { describePendingState, pollUntilTerminalOrPending, specialTools } from './specialTools.ts';

test('relaxed AIC MCP tool obtains cluster GPU budget and forwards original targets', async (t) => {
    const calls: {url: string; body?: Record<string, unknown>}[] = [];
    t.mock.method(globalThis, 'fetch', async (input, init) => {
        const url = String(input);
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        calls.push({url, body});
        if (url.includes('/api/cluster/overview')) {
            return Response.json({hardware: {gpuCount: 8, availableGpuCount: 4}});
        }
        return Response.json({anchor: null, attempts: [], candidates: []});
    });
    const tool = specialTools.find(item => item.name === 'search_aic_with_relaxation');
    assert.ok(tool);
    const result = await tool.handler({clusterId: 'cluster-a', workload: {model: 'model', ttftMs: 500}, searchConfig: {totalGpus: 8}});
    assert.equal(calls.length, 2);
    assert.match(calls[0].url, /\/api\/cluster\/overview\?clusterId=cluster-a/);
    assert.match(calls[1].url, /\/api\/candidate-search\/relaxed/);
    assert.deepEqual(calls[1].body, {workload: {model: 'model', ttftMs: 500}, searchConfig: {totalGpus: 4}, sourceIds: ['aic']});
    assert.deepEqual(result, {anchor: null, attempts: [], candidates: []});
});

test('describePendingState detects a top-level pending/queued/unschedulable status', () => {
    assert.equal(describePendingState({ status: 'pending' }), 'status=pending');
    assert.equal(describePendingState({ phase: 'Pending' }), 'phase=Pending');
    assert.equal(describePendingState({ state: 'queued' }), 'state=queued');
    assert.equal(describePendingState({ condition: 'unschedulable' }), 'condition=unschedulable');
});

test('describePendingState detects a pending pod nested one level down under a list wrapper', () => {
    // Shape returned by get_deployment_execution_pods: { pods: [{ phase, ... }] }.
    const result = { execution_id: 'abc', pods: [{ name: 'p1', phase: 'Running' }, { name: 'p2', phase: 'Pending' }] };
    assert.equal(describePendingState(result), 'pods[].phase=Pending');
});

test('describePendingState returns null for a terminal or otherwise unremarkable result', () => {
    assert.equal(describePendingState({ status: 'ready' }), null);
    assert.equal(describePendingState({ pods: [{ phase: 'Running' }] }), null);
    assert.equal(describePendingState(null), null);
    assert.equal(describePendingState('not an object'), null);
});

test('pollUntilTerminalOrPending returns immediately (without consuming the timeout) once a pending signal is seen', async () => {
    let calls = 0;
    const poll = async () => {
        calls += 1;
        return { execution_id: 'abc', pods: [{ name: 'p1', phase: 'Pending' }] };
    };
    // A long timeout/interval that would take far longer than this test's
    // own timeout if pollUntilTerminalOrPending actually waited for it --
    // proves the pending short-circuit fires on the very first poll instead
    // of silently blocking, which is the whole point of this behavior.
    const result = await pollUntilTerminalOrPending(poll, 600_000, 300_000);

    assert.equal(calls, 1);
    assert.deepEqual(result.done, false);
    assert.deepEqual(result.pending, true);
    assert.equal(result.attempts, 1);
    assert.match(result.message as string, /resource-constrained/);
});

test('pollUntilTerminalOrPending returns done:true as soon as a terminal state is seen, ignoring any pending signal', async () => {
    const poll = async () => ({ status: 'ready', pods: [{ phase: 'Pending' }] });
    const result = await pollUntilTerminalOrPending(poll, 600_000, 300_000);

    assert.equal(result.done, true);
    assert.equal(result.terminal, 'status=ready');
    assert.equal(result.pending, undefined);
});

test('pollUntilTerminalOrPending keeps polling past a plain non-terminal, non-pending state until it times out', async () => {
    let calls = 0;
    const poll = async () => {
        calls += 1;
        return { status: 'deploying' };
    };
    const result = await pollUntilTerminalOrPending(poll, 30, 20);

    assert.equal(result.done, false);
    assert.equal(result.pending, undefined);
    assert.ok(calls >= 1);
    assert.match(result.message as string, /Still not in a terminal state/);
});
