import assert from 'node:assert/strict';
import test from 'node:test';
import type { Response } from 'express';
import { localForwardedEndpoint, processPendingToolCalls, MCP_TOOL_CALL_TIMEOUT_MS, type OpenAiMessage, type OpenAiToolCall } from './chat.ts';

// A read-tier, write-tier, and approve-tier tool from the real registry
// (server/mcp/tools.ts) so `findTool()` resolves a real riskTier without
// needing to fake it.
const READ_TOOL = 'list_deploy_poc_configs';
const WRITE_TOOL = 'resolve_configurations';
const APPROVE_TOOL = 'delete_deployment_run';

test('Playground accepts only explicit HTTP loopback port-forward endpoints', () => {
    assert.equal(localForwardedEndpoint('http://127.0.0.1:43127'), 'http://127.0.0.1:43127');
    for (const value of [
        'http://127.0.0.1',
        'https://127.0.0.1:43127',
        'http://127.0.0.2:43127',
        'http://127.0.0.1.attacker.test:43127',
        'http://user@127.0.0.1:43127',
        'http://127.0.0.1:43127/admin',
        'http://127.0.0.1:43127/?redirect=1',
    ]) {
        assert.equal(localForwardedEndpoint(value), null, value);
    }
});

function toolCall(id: string, name: string): OpenAiToolCall {
    return { id, type: 'function', function: { name, arguments: '{}' } };
}

// Minimal fake standing in for the MCP `Client`: only `callTool` is used by
// processPendingToolCalls.
// eslint-disable-next-line no-unused-vars -- TypeScript function type parameter name.
function fakeClient(onCall?: (name: string) => void, onOptions?: (options: unknown) => void) {
    return {
        callTool: async ({ name }: { name: string }, _resultSchema: unknown, options: unknown) => {
            onCall?.(name);
            onOptions?.(options);
            return { ok: true, tool: name };
        },
    } as unknown as Parameters<typeof processPendingToolCalls>[2];
}

// Minimal fake standing in for Express' Response: only `write` is used (via
// sseWrite), and we capture each SSE event's parsed payload for assertions.
function fakeRes() {
    const events: Array<{ event: string; data: unknown }> = [];
    let pendingEvent = '';
    const res = {
        write(chunk: string) {
            if (chunk.startsWith('event: ')) {
                pendingEvent = chunk.slice('event: '.length).trim();
            } else if (chunk.startsWith('data: ')) {
                events.push({ event: pendingEvent, data: JSON.parse(chunk.slice('data: '.length)) });
            }
            return true;
        },
    } as unknown as Response;
    return { res, events };
}

test('a batch of [read, approve] tool_calls executes the read one then pauses on the approve one without touching later tool_calls', async () => {
    const conversation: OpenAiMessage[] = [];
    const calledTools: string[] = [];
    const client = fakeClient((name) => calledTools.push(name));
    const { res, events } = fakeRes();

    const toolCalls = [toolCall('call-1', READ_TOOL), toolCall('call-2', APPROVE_TOOL), toolCall('call-3', READ_TOOL)];
    const outcome = await processPendingToolCalls(toolCalls, conversation, client, res, false);

    assert.equal(outcome, 'paused');
    // Only the read tool before the approve one actually ran -- the read
    // tool AFTER the approve one must never run before the user has decided.
    assert.deepEqual(calledTools, [READ_TOOL]);
    // Exactly one tool response was appended (for call-1); call-2 and call-3
    // must have no tool response yet, since sending them back to the model
    // with a dangling tool_call_id is exactly the class of bug this guards.
    const toolMessages = conversation.filter((entry) => entry.role === 'tool');
    assert.deepEqual(toolMessages.map((entry) => entry.tool_call_id), ['call-1']);
    const confirmEvent = events.find((entry) => entry.event === 'confirm_required');
    assert.ok(confirmEvent);
    assert.equal((confirmEvent!.data as { id: string }).id, 'call-2');
});

test('a write-tier tool_call runs straight through like a read-tier one -- only approve-tier requires confirmation', async () => {
    const conversation: OpenAiMessage[] = [];
    const calledTools: string[] = [];
    const client = fakeClient((name) => calledTools.push(name));
    const { res, events } = fakeRes();

    const toolCalls = [toolCall('call-1', READ_TOOL), toolCall('call-2', WRITE_TOOL), toolCall('call-3', READ_TOOL)];
    const outcome = await processPendingToolCalls(toolCalls, conversation, client, res, false);

    assert.equal(outcome, 'continue');
    assert.deepEqual(calledTools, [READ_TOOL, WRITE_TOOL, READ_TOOL]);
    assert.equal(events.find((entry) => entry.event === 'confirm_required'), undefined);
    const toolMessages = conversation.filter((entry) => entry.role === 'tool');
    assert.deepEqual(toolMessages.map((entry) => entry.tool_call_id), ['call-1', 'call-2', 'call-3']);
});

test('resuming after approval must resolve every remaining tool_call in the same batch before the conversation is reused', async () => {
    // Simulates exactly the two-step flow in server.ts's /api/playground/chat
    // handler: (1) the initial batch pauses on the first approve-tier call,
    // recording the two tool_calls that came before/after it as `pending`;
    // (2) after the user approves, the resume code must resolve the
    // approved one AND the still-unresolved trailing one(s) before the
    // conversation is ever sent back to the model -- otherwise the next
    // model call 400s with "insufficient tool messages following
    // tool_calls message" (the exact bug this test reproduces/guards).
    const assistantMessage: OpenAiMessage = {
        role: 'assistant',
        content: null,
        tool_calls: [toolCall('call-1', READ_TOOL), toolCall('call-2', APPROVE_TOOL), toolCall('call-3', READ_TOOL)],
    };
    const conversation: OpenAiMessage[] = [{ role: 'user', content: 'do the thing' }];
    const calledTools: string[] = [];
    const client = fakeClient((name) => calledTools.push(name));
    const { res: res1 } = fakeRes();

    // Step 1: initial batch pauses at call-2.
    conversation.push(assistantMessage);
    const firstPause = await processPendingToolCalls(assistantMessage.tool_calls!, conversation, client, res1, false);
    assert.equal(firstPause, 'paused');

    // Step 2: emulate the resume handler in server.ts -- resolve the
    // approved tool_call (call-2), then resolve any siblings still missing
    // a tool response (call-3, which was never reached).
    conversation.push({ role: 'tool', tool_call_id: 'call-2', content: JSON.stringify({ approved: true }) });
    const { res: res2 } = fakeRes();
    const unresolved = assistantMessage.tool_calls!.filter(
        (entry) => !conversation.some((msg) => msg.role === 'tool' && msg.tool_call_id === entry.id),
    );
    assert.deepEqual(unresolved.map((entry) => entry.id), ['call-3']);
    const secondPause = await processPendingToolCalls(unresolved, conversation, client, res2, false);
    assert.equal(secondPause, 'continue');

    // Every tool_call from the original assistant message must now have
    // exactly one matching tool response -- this is the OpenAI API's hard
    // requirement that triggered the reported 400 when it was violated.
    const toolCallIds = assistantMessage.tool_calls!.map((entry) => entry.id);
    const toolResponseIds = conversation.filter((entry) => entry.role === 'tool').map((entry) => entry.tool_call_id);
    for (const id of toolCallIds) {
        assert.equal(toolResponseIds.filter((responseId) => responseId === id).length, 1, `expected exactly one tool response for ${id}`);
    }
    assert.deepEqual(calledTools, [READ_TOOL, READ_TOOL]);
});

test('client.callTool is invoked with a generous explicit timeout so long-running tools like wait_for_status are not aborted by the MCP SDK default (60s)', async () => {
    const conversation: OpenAiMessage[] = [];
    const capturedOptions: unknown[] = [];
    const client = fakeClient(undefined, (options) => capturedOptions.push(options));
    const { res } = fakeRes();

    await processPendingToolCalls([toolCall('call-1', READ_TOOL)], conversation, client, res, false);

    assert.equal(capturedOptions.length, 1);
    assert.equal((capturedOptions[0] as { timeout: number }).timeout, MCP_TOOL_CALL_TIMEOUT_MS);
    // Must comfortably exceed wait_for_status's own max timeoutSeconds (600s),
    // otherwise the fix regresses back to racing against the tool's internal wait.
    assert.ok(MCP_TOOL_CALL_TIMEOUT_MS > 600_000);
});

// A fake MCP `Client` whose callTool throws for a given set of tool names
// (simulating a transport failure / MCP -32603) and behaves like the normal
// fakeClient for everything else.
// eslint-disable-next-line no-unused-vars -- TypeScript function type parameter name.
function fakeThrowingClient(throwingToolNames: Set<string>, onCall?: (name: string) => void) {
    return {
        callTool: async ({ name }: { name: string }) => {
            onCall?.(name);
            if (throwingToolNames.has(name)) {
                throw new Error(`MCP error -32603: ${name} blew up`);
            }
            return { ok: true, tool: name };
        },
    } as unknown as Parameters<typeof processPendingToolCalls>[2];
}

test('a thrown client.callTool error is captured as an isError tool result instead of aborting the whole turn', async () => {
    const conversation: OpenAiMessage[] = [];
    const calledTools: string[] = [];
    const client = fakeThrowingClient(new Set([READ_TOOL]), (name) => calledTools.push(name));
    const { res, events } = fakeRes();

    // Two read-tier calls in the same batch: the first throws, the second
    // must still run -- a transport-level failure on one tool call must not
    // prevent the model from seeing sibling results or trying something else.
    const outcome = await processPendingToolCalls(
        [toolCall('call-1', READ_TOOL), toolCall('call-2', 'get_deployment_run')],
        conversation,
        client,
        res,
        false,
    );

    assert.equal(outcome, 'continue');
    assert.deepEqual(calledTools, [READ_TOOL, 'get_deployment_run']);

    const toolResultEvents = events.filter((entry) => entry.event === 'tool_result');
    assert.equal(toolResultEvents.length, 2);
    const failedEvent = toolResultEvents.find((entry) => (entry.data as { id: string }).id === 'call-1');
    assert.equal((failedEvent!.data as { isError: boolean }).isError, true);
    assert.match(
        ((failedEvent!.data as { result: { error: string } }).result).error,
        /MCP error -32603/,
    );

    // The model must receive a tool message for the failed call too (so it
    // can read the error and decide what to do next), not have the turn die
    // before a tool response is ever appended.
    const toolMessages = conversation.filter((entry) => entry.role === 'tool');
    assert.deepEqual(toolMessages.map((entry) => entry.tool_call_id), ['call-1', 'call-2']);
    const failedMessageContent = JSON.parse(toolMessages[0].content as string);
    assert.equal(failedMessageContent.isError, true);
    assert.match(failedMessageContent.error, /MCP error -32603/);
});

// A fake MCP `Client` that returns a Lens problem-details payload for one tool
// (wrapped the way server/mcp/server.ts wraps handler returns) and succeeds for
// everything else, so we can exercise the authorization-stop path.
// eslint-disable-next-line no-unused-vars -- TypeScript function type parameter name.
function fakeProblemClient(target: string, problem: Record<string, unknown>, onCall?: (name: string) => void) {
    return {
        callTool: async ({ name }: { name: string }) => {
            onCall?.(name);
            if (name !== target) return { content: [{ type: 'text', text: JSON.stringify({ ok: true }) }] };
            return {
                content: [{ type: 'text', text: JSON.stringify({ error: `upstream request failed (status ${problem.status})`, detail: problem }) }],
            };
        },
    } as unknown as Parameters<typeof processPendingToolCalls>[2];
}

test('a 403 tool result stops the batch, reports the missing permission, and skips later tool_calls', async () => {
    const conversation: OpenAiMessage[] = [];
    const calledTools: string[] = [];
    const client = fakeProblemClient(
        WRITE_TOOL,
        { status: 403, title: 'Forbidden', detail: 'Missing permission: configuration:artifact:save', code: 'forbidden' },
        (name) => calledTools.push(name),
    );
    const { res, events } = fakeRes();

    const outcome = await processPendingToolCalls(
        [toolCall('call-1', WRITE_TOOL), toolCall('call-2', READ_TOOL)],
        conversation,
        client,
        res,
        false,
    );

    assert.equal(outcome, 'denied');
    // The sibling after the denied call must not run.
    assert.deepEqual(calledTools, [WRITE_TOOL]);
    const denied = events.find((entry) => entry.event === 'permission_denied');
    assert.ok(denied);
    assert.equal((denied!.data as { requiredPermission?: string }).requiredPermission, 'configuration:artifact:save');
    assert.match((denied!.data as { message: string }).message, /Missing permission: configuration:artifact:save/);
    // Both tool_calls still get exactly one tool response so a later turn is valid.
    const toolMessages = conversation.filter((entry) => entry.role === 'tool');
    assert.deepEqual(toolMessages.map((entry) => entry.tool_call_id), ['call-1', 'call-2']);
    assert.match(JSON.parse(toolMessages[1].content as string).error, /not executed/);
});

test('a 401 tool result stops the turn with a re-login message', async () => {
    const client = fakeProblemClient(READ_TOOL, {
        status: 401,
        title: 'Unauthenticated',
        detail: 'A valid session is required',
        code: 'unauthenticated',
    });
    const conversation: OpenAiMessage[] = [];
    const { res, events } = fakeRes();

    const outcome = await processPendingToolCalls([toolCall('call-1', READ_TOOL)], conversation, client, res, false);

    assert.equal(outcome, 'denied');
    const denied = events.find((entry) => entry.event === 'permission_denied');
    assert.equal((denied!.data as { kind: string }).kind, 'unauthenticated');
    assert.match((denied!.data as { message: string }).message, /session has expired/i);
});
