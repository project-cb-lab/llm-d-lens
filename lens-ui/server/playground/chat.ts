// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// -----------------------------------------------------------------------------
// "Talk with your Prism" chat orchestrator (docs/design/mcp-playground-design.zh-CN.md
// section 3.1). This is the ONLY component that speaks both protocols:
//   - OpenAI-compatible chat/completions + tools/tool_calls, to the
//     Deployment the user picked in the Playground as the model service.
//   - MCP (list_tools / call_tool), to Prism's own MCP server (server/mcp).
// approve-tier tools are never auto-executed: the model's request to
// call one pauses the turn (a `confirm_required` SSE event, no tool run yet)
// until the user explicitly approves or rejects it from the UI, at which
// point the client resumes this same turn with `pendingResume` (see below).
// write-tier tools run straight through like read-tier ones -- only
// approve-tier actions (the riskier, harder-to-undo ones) need a human in
// the loop.
// -----------------------------------------------------------------------------

import { Router } from 'express';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { ProxyAgent } from 'undici';
import { internalBaseUrl, internalFetch, internalRequest } from '../mcp/internal.ts';
import { internalHeadersFor } from '../internalAuth.ts';
import { findTool, prismTools } from '../mcp/tools.ts';
import { findModelByRepository } from '../../src/data/modelCatalog.js';

export const playgroundRouter = Router();

// This process typically only reaches the public internet (huggingface.co,
// below) through an outbound HTTP(S) proxy -- unlike curl, Node's built-in
// fetch does NOT honor HTTPS_PROXY/HTTP_PROXY env vars on its own, so
// without this every HF Hub lookup would silently fail (fetch throws
// ETIMEDOUT) and discoverMaxModelLen would always bottom out at the 4096
// fallback. undici's ProxyAgent makes fetch proxy-aware; undefined (no
// dispatcher override) when no proxy is configured, i.e. unchanged behavior.
const HF_FETCH_DISPATCHER = (() => {
    const proxyUrl = process.env.https_proxy || process.env.HTTPS_PROXY || process.env.http_proxy || process.env.HTTP_PROXY;
    return proxyUrl ? new ProxyAgent(proxyUrl) : undefined;
})();


// Safety cap on how many model<->tool round-trips one chat turn can take
// before giving up, so a model stuck in a call/observe loop can't hang the
// request forever. Raised from 6, then from 20: real multi-step management/
// troubleshooting tasks (e.g. diagnosing a failed benchmark run across
// several clusters/deployments, with repeated wait_for_status polling) can
// legitimately need more hops than that.
const MAX_TOOL_CALL_TURNS = 40;

// The MCP SDK's own per-request timeout defaults to 60s (DEFAULT_REQUEST_TIMEOUT_MSEC),
// far too short for `wait_for_status` (server/mcp/tools.ts), which
// deliberately blocks for up to its own `timeoutSeconds` argument (max 600s)
// while polling a long-running operation server-side. Without overriding it
// here, the MCP client itself aborts the call with a generic MCP error
// -32001 "Request timed out" well before wait_for_status ever gets to
// return -- even though the tool call would otherwise have succeeded.
// Applied to every tool call (not just wait_for_status) since any Lens tool
// could in principle be slow; comfortably above wait_for_status's own
// 600s cap, with headroom for the poll loop's own bookkeeping.
export const MCP_TOOL_CALL_TIMEOUT_MS = 630_000;

// The only system-level guidance injected into a Talk with Lens
// conversation: prompted separately from (and in addition to) the tool
// descriptions themselves, since a model can otherwise happily keep polling
// wait_for_status in silence for minutes on a deployment that's simply
// stuck queued waiting for free cluster resources, without ever telling
// the user why -- or, on a tool error, just give up and dump a raw failure
// instead of trying another approach or explaining what happened -- and, on an
// authorization error specifically, must stop instead of retrying. Kept
// short and narrowly scoped to these behaviors rather than a
// general-purpose "how to use tools" prompt, so it can't drift out of sync
// with the tool descriptions that already cover the rest.
const SYSTEM_PROMPT =
    'When you start a deployment (or any long-running Lens operation) and check its status, if the ' +
    'status/pods look "pending", "queued", or "unschedulable" (e.g. a Kubernetes pod phase of Pending, or ' +
    'wait_for_status returning pending: true), tell the user right away that it looks resource-constrained ' +
    'and the request is queued -- do not silently keep waiting for it to finish. Only keep polling if the ' +
    'user asks you to keep monitoring.\n\n' +
    'While a deployment is otherwise actively starting up (not stuck pending) and you are monitoring it, do ' +
    'not silently poll wait_for_status once with a long timeout and go quiet until it finishes. Instead, poll ' +
    'with a short timeoutSeconds (roughly 30-60s) each call, and every time you are about to poll again, ' +
    'include a brief plain-language status update in that same reply alongside the tool call -- e.g. "still ' +
    'starting up, 1/3 pods ready, checking again..." -- so the user sees periodic progress instead of one ' +
    'long silence followed by a final answer. Keep each update short; only stop polling and give your final ' +
    'answer once the deployment reaches a terminal state (ready, succeeded, failed, etc.) or the user asked ' +
    'you to stop.\n\n' +
    'Authorization errors are final: if a tool returns an HTTP 401 or 403, or a body whose code is ' +
    '"forbidden", "unauthenticated" or "password_change_required", or a detail like "Missing permission: X" ' +
    'or "permission denied", do NOT retry, do NOT try other tools or arguments, and never claim the action ' +
    'succeeded. Stop immediately and tell the user plainly that they lack the required permission (quote the ' +
    'permission code when one is given) and suggest asking an administrator. This overrides the general ' +
    'error-recovery guidance below.\n\n' +
    'If a tool call fails or comes back with an error and it is NOT an authorization error, do not just give ' +
    'up and report a raw error. First think about whether a different tool, different arguments, or a ' +
    'different order of operations could still accomplish what the user asked, and try that. Only if you ' +
    'genuinely cannot make progress should you stop -- and in that case, explain in plain language what you ' +
    'were trying to do, what went wrong, and include the actual error message so the user (or their operator) ' +
    'can act on it, instead of just surfacing a bare failure.\n\n' +
    'When the user asks you to deploy a model, prefer the Agentic Deployment tools (create_agentic_plan, ' +
    'list_agentic_plans, get_agentic_plan, select_agentic_plan_candidate, refine_agentic_plan, ' +
    'approve_agentic_plan) over starting a deployment directly, since the planner can propose and score ' +
    'multiple candidate topologies instead of committing to just one guess. If the user states any ' +
    'preference or opinion about the deployment (e.g. prioritize latency vs. throughput, a specific replica ' +
    'count or parallelism, disaggregated prefill/decode, a GPU/hardware constraint, cost sensitivity, etc.), ' +
    'pass it along as the `planner_prompt` on create_agentic_plan (or refine_agentic_plan for an existing ' +
    'plan) so the planner actually accounts for it -- do not silently decide on the user\'s behalf. Once a ' +
    'plan comes back with candidates, present them to the user in plain language (key differences like ' +
    'replicas, tensor parallelism, prefill/decode split, and their supporting evidence) and ask which one ' +
    'they want -- do not pick a candidate yourself and proceed straight to approval. Only call ' +
    'select_agentic_plan_candidate after the user has told you which candidate they want, and only call ' +
    'approve_agentic_plan once the user has explicitly confirmed they want to deploy that selected ' +
    'candidate.';

// Fallback assumption when none of the sources in discoverMaxModelLen below
// can tell us the model's real context window -- deliberately conservative:
// better to trim proactively than to hit a hard "context length exceeded"
// error from the model server.
const DEFAULT_MAX_MODEL_LEN = 4096;
// Timeout for the best-effort, unauthenticated HuggingFace Hub config.json
// fetch below -- this must never meaningfully delay a chat turn.
const HF_CONFIG_FETCH_TIMEOUT_MS = 4000;
// Common field names used across HuggingFace `config.json` files to record
// a model's maximum context length, most-specific first.
const HF_CONFIG_CONTEXT_LENGTH_KEYS = [
    'max_position_embeddings',
    'n_positions',
    'max_sequence_length',
    'seq_length',
    'model_max_length',
];
// Multimodal/composite HF configs (e.g. DeepSeek-V4.1-Flash, many VLMs) nest
// the text decoder's own config -- and therefore its context length field --
// under one of these sub-objects instead of the top level. Without checking
// these too, such models would always miss HF_CONFIG_CONTEXT_LENGTH_KEYS and
// silently fall back to DEFAULT_MAX_MODEL_LEN even though the real context
// length is right there in the config.
const HF_CONFIG_NESTED_CONTEXT_KEYS = ['text_config', 'llm_config', 'language_config'];

// Start trimming once the estimated prompt would use this fraction of the
// model's context window, leaving headroom for the model's own reply.
const CONTEXT_BUDGET_RATIO = 0.8;
// Tool results (cluster/deployment listings, etc.) can be large JSON blobs;
// cap each one's contribution to the prompt at this many characters before
// falling back to dropping whole history messages.
const TOOL_RESULT_TRUNCATE_CHARS = 1500;

// Rough, tokenizer-free token estimate: good enough for a soft context
// budget check, not an exact count. Plain ASCII/English text averages ~4
// characters per token, but Prism operators here also chat in Chinese,
// where each character is usually close to its own token -- so CJK
// characters are counted individually and everything else at chars/4.
function estimateTokens(text: string | null | undefined): number {
    if (!text) return 0;
    const cjkMatches = text.match(/[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]/g);
    const cjkCount = cjkMatches ? cjkMatches.length : 0;
    const otherCount = text.length - cjkCount;
    return Math.ceil(cjkCount + otherCount / 4);
}

function estimateMessageTokens(message: OpenAiMessage): number {
    let total = estimateTokens(message.content) + estimateTokens(message.reasoning_content) + 4; // small per-message overhead
    for (const toolCall of message.tool_calls ?? []) {
        total += estimateTokens(toolCall.function.name) + estimateTokens(toolCall.function.arguments);
    }
    return total;
}

function estimateConversationTokens(conversation: OpenAiMessage[]): number {
    return conversation.reduce((sum, message) => sum + estimateMessageTokens(message), 0);
}

// Best-effort resolution of a model name that ISN'T already a HF repo id
// (e.g. an External AI provider's own product name like "deepseek-v4-pro")
// to the actual HF repo that publishes it (e.g. "deepseek-ai/DeepSeek-V4-Pro"),
// via HF Hub's public, unauthenticated search API. Never throws.
async function searchHuggingFaceRepoId(model: string): Promise<string | undefined> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), HF_CONFIG_FETCH_TIMEOUT_MS);
    try {
        const response = await fetch(
            `https://huggingface.co/api/models?search=${encodeURIComponent(model)}&limit=1`,
            { signal: controller.signal, dispatcher: HF_FETCH_DISPATCHER } as RequestInit,
        );
        if (!response.ok) return undefined;
        const results = (await response.json()) as Array<{ id?: string }>;
        return results[0]?.id;
    } catch {
        return undefined;
    } finally {
        clearTimeout(timeout);
    }
}

// Reads a HF config.json's context length, checking both top-level fields
// and the nested sub-configs multimodal models use (see
// HF_CONFIG_NESTED_CONTEXT_KEYS above).
function extractHfContextLength(config: Record<string, unknown>): number | undefined {
    for (const key of HF_CONFIG_CONTEXT_LENGTH_KEYS) {
        const value = config[key];
        if (typeof value === 'number' && value > 0) return value;
    }
    for (const nestedKey of HF_CONFIG_NESTED_CONTEXT_KEYS) {
        const nested = config[nestedKey];
        if (nested && typeof nested === 'object') {
            const value = extractHfContextLength(nested as Record<string, unknown>);
            if (value) return value;
        }
    }
    return undefined;
}

// Best-effort lookup of `model`'s published context length from
// HuggingFace's public, unauthenticated config.json. `model` may already be
// a HF repo id (e.g. "deepseek-ai/DeepSeek-V4-Pro") or an External AI
// provider's own product name (e.g. "deepseek-v4-pro"), in which case we
// first resolve it to a repo id via HF's search API. Never throws: a
// network failure, no search match, or unrecognized config shape just means
// "we don't know."
async function fetchHuggingFaceContextLength(model: string | undefined): Promise<number | undefined> {
    if (!model) return undefined;
    const repoId = /^[\w.-]+\/[\w.-]+$/.test(model) ? model : await searchHuggingFaceRepoId(model);
    if (!repoId) return undefined;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), HF_CONFIG_FETCH_TIMEOUT_MS);
    try {
        const response = await fetch(`https://huggingface.co/${repoId}/raw/main/config.json`, {
            signal: controller.signal,
            dispatcher: HF_FETCH_DISPATCHER,
        } as RequestInit);
        if (!response.ok) return undefined;
        const config = (await response.json()) as Record<string, unknown>;
        return extractHfContextLength(config);
    } catch {
        return undefined;
    } finally {
        clearTimeout(timeout);
    }
}

// Discovers the selected Deployment/provider's real context window from its
// `model` name directly -- no need to probe the endpoint (e.g. vLLM's
// GET /v1/models) since the model name is already known up front. Tries (in
// order, cheapest/most-authoritative first):
//   1. Lens's Model Market catalog (src/data/modelCatalog.js), which already
//      records a HuggingFace-sourced contextLength for every cataloged repo.
//   2. HuggingFace Hub's public config.json, resolving `model` to a repo id
//      via HF's search API first when it isn't already one.
//   3. DEFAULT_MAX_MODEL_LEN, when neither of the above knew the answer.
async function discoverMaxModelLen(model?: string): Promise<number> {
    const catalogEntry = findModelByRepository(model);
    if (typeof catalogEntry?.contextLength === 'number' && catalogEntry.contextLength > 0) {
        return catalogEntry.contextLength;
    }
    const hfContextLength = await fetchHuggingFaceContextLength(model);
    if (hfContextLength) return hfContextLength;
    return DEFAULT_MAX_MODEL_LEN;
}

// Truncates any oversized 'tool' role message content in place. Returns
// whether anything was actually truncated (so the caller only needs to
// re-estimate tokens, and can tell the user, when this did something).
function truncateOversizedToolResults(conversation: OpenAiMessage[]): boolean {
    let truncatedAny = false;
    for (const message of conversation) {
        if (message.role === 'tool' && typeof message.content === 'string' && message.content.length > TOOL_RESULT_TRUNCATE_CHARS) {
            const droppedChars = message.content.length - TOOL_RESULT_TRUNCATE_CHARS;
            message.content = `${message.content.slice(0, TOOL_RESULT_TRUNCATE_CHARS)}\n...[truncated ${droppedChars} more characters to fit the model's context window]`;
            truncatedAny = true;
        }
    }
    return truncatedAny;
}

// No GET /api/playground/deployments here: the frontend calls the real
// GET /api/v1/deployments/executions (llm_d_bench/deploy/router.py) directly
// via listDeploymentExecutions() in remoteDeployBackend.js, the same helper
// the Optimization Deployments page already uses. No bespoke wrapper API.

export interface OpenAiToolCall {
    id: string;
    type: 'function';
    function: { name: string; arguments: string };
}

export interface OpenAiMessage {
    role: string;
    content: string | null;
    // Reasoning models served through OpenAI-compatible APIs (e.g. vLLM with
    // DeepSeek-R1 style models) return their chain-of-thought separately from
    // the final answer in this field. Surfaced to the UI as a distinct
    // "think" event so it can be rendered collapsed by default.
    reasoning_content?: string | null;
    tool_calls?: OpenAiToolCall[];
    tool_call_id?: string;
}

// Sent by the client instead of `messages` to resume a turn that was paused
// on a `confirm_required` event: `conversation` is the exact resume blob that
// event carried back (the full OpenAI-format conversation up to and
// including the assistant message with the pending tool_calls), `toolCallId`
// identifies which of those tool_calls the user just decided on.
interface PendingResume {
    conversation: OpenAiMessage[];
    toolCallId: string;
    decision: 'approve' | 'reject';
}

function sseWrite(res: import('express').Response, event: string, data: unknown) {
    res.write(`event: ${event}\n`);
    res.write(`data: ${JSON.stringify(data)}\n\n`);
    // The server enables gzip compression globally (see server.js), which by
    // default buffers writes until enough data accumulates -- fine for a
    // normal JSON response, but it defeats the whole point of an SSE stream:
    // events would sit in the gzip buffer instead of reaching the browser as
    // each think/tool_call/progress step actually happens. `compression`
    // attaches a `flush()` that forces the current buffer out immediately.
    (res as unknown as { flush?: () => void }).flush?.();
}

// A tool call can fail two different ways: the MCP call itself throws (e.g.
// the transport dropped, the server process errored out, a -32603 "Internal
// server error") or it resolves normally with `isError: true` (e.g. an
// upstream Lens API returned a 4xx/5xx, wrapped as a plain error payload).
// Both should look the same to the model and to the UI -- a normal tool
// result carrying error details -- rather than the first kind aborting the
// whole chat turn with a raw, un-actionable SSE `error` event. Wrapping every
// callTool() through here means the model always gets a chance to read the
// error and decide what to do next (retry differently, or explain the
// problem to the user) instead of the conversation just dying mid-turn.
async function callToolSafely(client: Client, name: string, args: Record<string, unknown>): Promise<{ result: unknown; isError: boolean }> {
    try {
        const result = (await client.callTool({ name, arguments: args }, undefined, { timeout: MCP_TOOL_CALL_TIMEOUT_MS })) as { isError?: boolean };
        return { result, isError: Boolean(result?.isError) };
    } catch (error) {
        return {
            result: { isError: true, error: error instanceof Error ? error.message : String(error) },
            isError: true,
        };
    }
}

// Runs (or blocks/pauses-for-confirmation) each tool_call in order, pushing a
// matching `role: 'tool'` message into `conversation` for every one that's
// resolved synchronously here (executed or blocked). Returns `true` the
// moment an approve-tier tool_call is hit and a confirmation is needed:
// the caller must stop immediately (no further tool_calls in this batch are
// touched, and no further model calls are made) until the client resumes.
// Every OpenAI-compatible chat/completions API requires an assistant message
// with tool_calls to be immediately followed by exactly one tool message per
// tool_call_id -- if any sibling tool_call from the same batch were left
// unresolved when the conversation is next sent back to the model, that
// model call would 400 with "insufficient tool messages following
// tool_calls message". Used both for a fresh batch of tool_calls and (after
// resolving the one the user just approved/rejected) for any siblings a
// prior pause left unresolved -- see the `pendingResume` handling below.
export type ToolBatchOutcome = 'continue' | 'paused' | 'denied';

type PermissionFailure = {
    kind: 'forbidden' | 'unauthenticated' | 'password_change_required' | 'cluster_not_accessible';
    requiredPermission?: string;
};

// Read the JSON payload an MCP tool result wraps in its text content.
function toolResultText(result: unknown): string | null {
    if (typeof result === 'string') return result;
    if (!result || typeof result !== 'object') return null;
    const content = (result as { content?: unknown }).content;
    if (!Array.isArray(content)) return null;
    for (const item of content) {
        if (item && typeof item === 'object' && (item as { type?: unknown }).type === 'text') {
            const text = (item as { text?: unknown }).text;
            if (typeof text === 'string') return text;
        }
    }
    return null;
}

// Classify an authorization failure from a tool result. Every tool delegates to
// a permission-checked Lens route (`internalRequest`/`jsonRequest`), so a 401 or
// 403 here is a final authorization outcome, not a retryable error. `csrf_failed`
// is a server-side transport problem, not a user permission issue, so it is not
// treated as final here.
export function detectPermissionFailure(result: unknown): PermissionFailure | null {
    const text = toolResultText(result);
    if (!text) return null;
    let payload: unknown;
    try {
        payload = JSON.parse(text);
    } catch {
        return null;
    }
    if (!payload || typeof payload !== 'object') return null;
    const body = payload as Record<string, unknown>;
    const problem = body.detail && typeof body.detail === 'object' ? (body.detail as Record<string, unknown>) : body;
    const status = Number(problem.status ?? body.status);
    const code = String(problem.code ?? body.code ?? '');
    const detailText = typeof problem.detail === 'string'
        ? problem.detail
        : typeof body.detail === 'string' ? body.detail : '';
    if (status === 401 || code === 'unauthenticated') return { kind: 'unauthenticated' };
    if (status !== 403) return null;
    if (code === 'csrf_failed') return null;
    if (code === 'password_change_required') return { kind: 'password_change_required' };
    if (/cluster is not accessible/i.test(detailText)) return { kind: 'cluster_not_accessible' };
    const match = /Missing permission:\s*([^\s"']+)/i.exec(detailText);
    return { kind: 'forbidden', requiredPermission: match?.[1] };
}

function permissionMessage(toolName: string, failure: PermissionFailure): string {
    switch (failure.kind) {
        case 'unauthenticated':
            return 'Your session has expired. Please sign in again.';
        case 'password_change_required':
            return 'You must change your password before Lens can run that action.';
        case 'cluster_not_accessible':
            return `You do not have access to the cluster required by "${toolName}".`;
        default:
            return failure.requiredPermission
                ? `You do not have permission to run "${toolName}". Missing permission: ${failure.requiredPermission}. Ask an administrator to grant it.`
                : `You do not have permission to run "${toolName}" (permission denied). Ask an administrator.`;
    }
}

export async function processPendingToolCalls(
    toolCalls: OpenAiToolCall[],
    conversation: OpenAiMessage[],
    client: Client,
    res: import('express').Response,
    readOnly: boolean,
): Promise<ToolBatchOutcome> {
    for (let index = 0; index < toolCalls.length; index += 1) {
        const toolCall = toolCalls[index];
        const toolName = toolCall.function.name;
        const definition = findTool(toolName);
        let args: Record<string, unknown> = {};
        try {
            args = toolCall.function.arguments ? JSON.parse(toolCall.function.arguments) : {};
        } catch {
            args = {};
        }

        if (!definition) {
            sseWrite(res, 'blocked', { name: toolName, arguments: args, reason: 'unknown tool' });
            conversation.push({
                role: 'tool',
                tool_call_id: toolCall.id,
                content: JSON.stringify({ error: 'blocked: unknown tool' }),
            });
            continue;
        }

        if (definition.riskTier !== 'read' && readOnly) {
            // Voice-originated turn: never even offer a confirmation for
            // a write/approve-tier tool, hard block it instead (covers a
            // hallucinated tool_call for a name that wasn't in the
            // filtered tools list sent to the model above).
            sseWrite(res, 'blocked', {
                name: toolName,
                arguments: args,
                reason: 'this turn started from voice input and is restricted to read-only tools',
            });
            conversation.push({
                role: 'tool',
                tool_call_id: toolCall.id,
                content: JSON.stringify({ error: 'blocked: voice-originated turns cannot run write/approve-tier tools' }),
            });
            continue;
        }

        if (definition.riskTier === 'approve') {
            // Pause here: the conversation so far (including the assistant
            // message and any already-executed sibling tool_calls from the
            // same batch) is sent back as the resume blob. The turn
            // continues only if/when the client POSTs back with
            // { pendingResume }. Only the approve tier (the riskier,
            // harder-to-undo actions) requires this -- write-tier tools run
            // straight through like read-tier ones, same as any other
            // auto-executed tool call below.
            sseWrite(res, 'confirm_required', {
                id: toolCall.id,
                name: toolName,
                arguments: args,
                riskTier: definition.riskTier,
                description: definition.description,
                resumeConversation: conversation,
            });
            return 'paused';
        }

        sseWrite(res, 'tool_call', { id: toolCall.id, name: toolName, arguments: args });
        const { result, isError } = await callToolSafely(client, toolName, args);
        sseWrite(res, 'tool_result', { id: toolCall.id, name: toolName, result, isError });
        conversation.push({
            role: 'tool',
            tool_call_id: toolCall.id,
            content: JSON.stringify(result),
        });

        const failure = detectPermissionFailure(result);
        if (failure) {
            // Authorization is final: stop the turn now instead of handing the
            // error back to the model to retry, and resolve the remaining
            // siblings so the conversation stays valid for a later turn.
            sseWrite(res, 'permission_denied', {
                id: toolCall.id,
                name: toolName,
                kind: failure.kind,
                requiredPermission: failure.requiredPermission,
                message: permissionMessage(toolName, failure),
            });
            for (let rest = index + 1; rest < toolCalls.length; rest += 1) {
                conversation.push({
                    role: 'tool',
                    tool_call_id: toolCalls[rest].id,
                    content: JSON.stringify({ error: 'not executed: a previous tool call was denied by permissions' }),
                });
            }
            return 'denied';
        }
    }
    return 'continue';
}

// Some reasoning models (served through plain OpenAI-compatible endpoints,
// without a separate reasoning_content field) emit their chain-of-thought
// inline as a <think>...</think> block at the start of message.content. Pull
// that out so the UI can show it as its own collapsed "think" section
// instead of leaking the tags into the final answer.
function splitInlineThink(content: string | null | undefined): { think: string | null; answer: string | null } {
    if (!content) return { think: null, answer: content ?? null };
    const match = content.match(/<think>([\s\S]*?)<\/think>/i);
    if (!match || match.index === undefined) return { think: null, answer: content };
    const think = match[1].trim();
    const answer = (content.slice(0, match.index) + content.slice(match.index + match[0].length)).trim();
    return { think: think || null, answer };
}

export function localForwardedEndpoint(value: unknown): string | null {
    if (typeof value !== 'string') return null;
    let endpoint: URL;
    try {
        endpoint = new URL(value);
    } catch {
        return null;
    }
    if (endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1' || !endpoint.port
        || endpoint.username || endpoint.password || endpoint.pathname !== '/' || endpoint.search || endpoint.hash) {
        return null;
    }
    return endpoint.origin;
}

// `target` is either an internal Deployment's reachable endpoint, or the ID
// of a saved External AI provider (llm_d_bench/ai_providers). For the latter,
// the actual outbound HTTP call (and the real API key) never leaves the
// Python backend process -- see proxy_chat_completions in
// llm_d_bench/ai_providers/router.py -- so this function only ever sees the
// provider's JSON response, never its key.
async function callDeployment(
    target: { endpoint?: string; providerId?: string },
    model: string,
    apiKey: string | undefined,
    messages: OpenAiMessage[],
    tools: unknown[],
) {
    const body = { model, messages, tools: tools.length ? tools : undefined, tool_choice: tools.length ? 'auto' : undefined };

    if (target.providerId) {
        const result = await internalRequest(`/api/v1/ai-providers/${encodeURIComponent(target.providerId)}/chat/completions`, {
            method: 'POST',
            body: JSON.stringify(body),
        });
        if (!result.ok) {
            const text = typeof result.body === 'string' ? result.body : JSON.stringify(result.body);
            throw new Error(`External AI provider returned ${result.status}: ${(text || '').slice(0, 500)}`);
        }
        return result.body as { choices: Array<{ message: OpenAiMessage }> };
    }

    const url = `${(target.endpoint as string).replace(/\/$/, '')}/v1/chat/completions`;
    const headers: Record<string, string> = { 'content-type': 'application/json' };
    if (apiKey) headers.authorization = `Bearer ${apiKey}`;
    const response = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) });
    if (!response.ok) {
        const text = await response.text().catch(() => '');
        // vLLM serves OpenAI's tool-calling API but only accepts tool_choice
        // when the server itself was started with --enable-auto-tool-choice
        // --tool-call-parser <parser>. Many known-good baseline deployments
        // (e.g. the Model Market Qwen3-0.6B baseline) are not, so surface this
        // as a distinct, recoverable condition instead of a generic failure.
        if (response.status === 400 && /tool.choice/i.test(text) && /enable-auto-tool-choice/i.test(text)) {
            throw new ToolCallingUnsupportedError(text.slice(0, 500));
        }
        throw new Error(`Deployment endpoint returned ${response.status}: ${text.slice(0, 500)}`);
    }
    return response.json() as Promise<{ choices: Array<{ message: OpenAiMessage }> }>;
}

class ToolCallingUnsupportedError extends Error {}

// POST a chat turn. Body is either:
//   { deployment: { executionId | providerId, model, apiKey? }, messages: [...] }
// for a fresh user message, or
//   { deployment: {...}, pendingResume: { conversation, toolCallId, decision } }
// to resume a turn paused on a `confirm_required` event (see below).
// "AI provider" mode in the UI: Internal (executionId, a Deployment
// the user runs) or External (providerId, a saved External AI provider --
// see llm_d_bench/ai_providers). Streams Server-Sent Events: think,
// tool_call, tool_result, blocked, confirm_required, progress, message, error, done.
playgroundRouter.post('/api/playground/chat', async (req, res) => {
    const deployment = req.body?.deployment as
        { executionId?: string; providerId?: string; model?: string; apiKey?: string } | undefined;
    const messages = Array.isArray(req.body?.messages) ? (req.body.messages as OpenAiMessage[]) : [];
    const pendingResume = req.body?.pendingResume as PendingResume | undefined;
    // Voice-transcribed messages can't be proofread by the user before
    // sending (see PlaygroundPage.jsx's mic handler) -- restrict tool use
    // for that whole turn to read-tier Lens MCP tools, so a misheard word
    // can never even reach a write/approve-tier tool, confirmation or not.
    const readOnly = Boolean(req.body?.readOnly);
    if ((!deployment?.executionId && !deployment?.providerId) || !deployment?.model
        || (deployment.executionId && deployment.providerId)) {
        return res.status(400).json({
            error: 'Exactly one of deployment.executionId or deployment.providerId (plus deployment.model) is required',
        });
    }
    if (pendingResume) {
        if (!Array.isArray(pendingResume.conversation) || !pendingResume.conversation.length || !pendingResume.toolCallId
            || (pendingResume.decision !== 'approve' && pendingResume.decision !== 'reject')) {
            return res.status(400).json({
                error: 'pendingResume must include a non-empty conversation, a toolCallId, and decision "approve" or "reject"',
            });
        }
    } else if (!messages.length) {
        return res.status(400).json({ error: 'messages must be a non-empty array' });
    }

    // The endpoint returned by list_ready_deployments is a Kubernetes-internal
    // ClusterIP address (`*.svc:port`) that the Prism backend process cannot
    // reach directly. Resolve it to a live kubectl port-forward on 127.0.0.1
    // first (same mechanism Model Market uses via connectDeploymentExecution).
    // Not needed for External AI providers: those are called by their real,
    // publicly-reachable base URL server-side (see callDeployment above).
    let endpoint: string | undefined;
    if (deployment.executionId) {
        const resolved = await internalRequest(`/api/v1/deployments/executions/${encodeURIComponent(deployment.executionId)}/endpoint`, {
            method: 'POST',
            body: JSON.stringify({}),
        });
        if (!resolved.ok) {
            res.setHeader('Content-Type', 'text/event-stream');
            res.setHeader('Cache-Control', 'no-cache');
            res.setHeader('Connection', 'keep-alive');
            // Tell any reverse proxy (e.g. nginx) in front of this server not
            // to buffer the response either -- otherwise events would still
            // arrive in a lump even with compression's own buffering fixed.
            res.setHeader('X-Accel-Buffering', 'no');
            res.flushHeaders?.();
            sseWrite(res, 'error', { message: `Failed to connect to the selected Deployment (status ${resolved.status}).` });
            sseWrite(res, 'done', {});
            return res.end();
        }
        const body = resolved.body as { forwarded_endpoint?: unknown };
        endpoint = localForwardedEndpoint(body.forwarded_endpoint) || undefined;
        if (!endpoint) {
            return res.status(502).json({ error: 'The selected Deployment did not resolve to a local port-forward.' });
        }
    }
    if (!endpoint && !deployment.providerId) {
        return res.status(400).json({ error: 'could not resolve a reachable endpoint for the selected Deployment' });
    }

    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.setHeader('X-Accel-Buffering', 'no');
    res.flushHeaders?.();

    let client: Client | undefined;
    try {
        client = new Client({ name: 'prism-playground', version: '0.1.0' });
        // The MCP server sits behind the same gateway, so this loopback call must
        // carry the current user's identity instead of an anonymous request.
        const mcpFetch: typeof fetch = (input, init) => {
            const headers = new Headers(init?.headers);
            const method = (init?.method || 'GET').toUpperCase();
            for (const [name, value] of Object.entries(internalHeadersFor(method, '/api/mcp'))) {
                headers.set(name, value);
            }
            return internalFetch(input, { ...init, headers });
        };
        const transport = new StreamableHTTPClientTransport(new URL(`${internalBaseUrl()}/api/mcp`), { fetch: mcpFetch });
        await client.connect(transport);

        const { tools: mcpTools } = await client.listTools();
        const openAiTools = mcpTools
            .filter((tool) => !readOnly || findTool(tool.name)?.riskTier === 'read')
            .map((tool) => ({
                type: 'function' as const,
                function: { name: tool.name, description: tool.description, parameters: tool.inputSchema },
            }));

        const maxModelLen = await discoverMaxModelLen(deployment.model);
        const contextBudget = Math.floor(maxModelLen * CONTEXT_BUDGET_RATIO);

        const conversation: OpenAiMessage[] = pendingResume ? [...pendingResume.conversation] : [...messages];
        // Only prepend on a fresh conversation -- a resumed one already
        // carries it from when this same turn was first paused, and
        // prepending again would duplicate it.
        if (conversation[0]?.role !== 'system') {
            conversation.unshift({ role: 'system', content: SYSTEM_PROMPT });
        }
        let pausedForConfirmation = false;
        let turnStopped = false;

        if (pendingResume) {
            // Resolve the one pending tool_call the user just approved or
            // rejected before re-entering the normal loop below. It must
            // belong to the LAST assistant message in the resume blob --
            // that's the one whose tool_calls caused the pause.
            const lastAssistantMessage = [...conversation].reverse().find((entry) => entry.role === 'assistant' && entry.tool_calls?.length);
            const toolCall = lastAssistantMessage?.tool_calls?.find((entry) => entry.id === pendingResume.toolCallId);
            if (!toolCall) {
                sseWrite(res, 'error', { message: 'Could not find the pending tool call to resolve -- it may have already been handled.' });
                sseWrite(res, 'done', {});
                return res.end();
            }
            const toolName = toolCall.function.name;
            const definition = findTool(toolName);
            let args: Record<string, unknown> = {};
            try {
                args = toolCall.function.arguments ? JSON.parse(toolCall.function.arguments) : {};
            } catch {
                args = {};
            }
            if (pendingResume.decision === 'approve' && definition && definition.riskTier === 'approve') {
                const { result, isError } = await callToolSafely(client, toolName, args);
                sseWrite(res, 'tool_result', { id: toolCall.id, name: toolName, result, isError });
                conversation.push({ role: 'tool', tool_call_id: toolCall.id, content: JSON.stringify(result) });
            } else {
                const rejectionResult = {
                    rejected: true,
                    reason: pendingResume.decision === 'approve'
                        ? 'this tool is no longer registered or is not approvable and was not executed'
                        : 'the user rejected this action; it was not executed',
                };
                sseWrite(res, 'tool_result', { id: toolCall.id, name: toolName, result: rejectionResult });
                conversation.push({ role: 'tool', tool_call_id: toolCall.id, content: JSON.stringify(rejectionResult) });
            }

            // The pause only ever happens at the FIRST approve-tier
            // tool_call in a batch (see processPendingToolCalls below), so
            // any sibling tool_calls from that same assistant message after
            // it -- and the one just resolved above -- may still be missing
            // their required tool response. Resolve them now, in order,
            // before this conversation is ever sent back to the model:
            // otherwise a batch of e.g. [read, approve, approve] would leave
            // the second approve tool_call dangling and the next model call
            // would 400 with "insufficient tool messages following
            // tool_calls message".
            const unresolved = (lastAssistantMessage?.tool_calls ?? []).filter(
                (entry) => !conversation.some((msg) => msg.role === 'tool' && msg.tool_call_id === entry.id),
            );
            if (unresolved.length) {
                const outcome = await processPendingToolCalls(unresolved, conversation, client, res, readOnly);
                if (outcome === 'paused') pausedForConfirmation = true;
                if (outcome === 'denied') turnStopped = true;
            }
        }

        // Some Deployments' vLLM servers were not started with
        // --enable-auto-tool-choice --tool-call-parser, so they 400 on any
        // request carrying tool_choice. Degrade gracefully to a tool-less
        // conversation instead of failing the whole chat turn.
        let toolsEnabled = openAiTools.length > 0;
        let turn = 0;
        let finalAnswerText: string | undefined;
        outerLoop: while (!pausedForConfirmation && !turnStopped && turn < MAX_TOOL_CALL_TURNS) {
            turn += 1;

            // Keep the CURRENT turn's own prompt from hard-failing as tool
            // results accumulate: this only clamps any single oversized tool
            // result in place, it never drops whole messages, so it can't
            // disrupt the answer the model is actively building this turn.
            // Dropping older conversation history ("compaction" proper) is
            // deliberately deferred until after the model fully answers this
            // question (see below, after the loop) so it never interrupts a
            // question that's still being worked on.
            const toolsTokens = toolsEnabled ? estimateTokens(JSON.stringify(openAiTools)) : 0;
            if (toolsTokens + estimateConversationTokens(conversation) > contextBudget) {
                truncateOversizedToolResults(conversation);
            }

            // Surface current context-window usage to the UI (small
            // number + pie indicator next to the input box) so it reflects
            // the real, post-truncation prompt size for this turn.
            sseWrite(res, 'context_usage', {
                maxModelLen,
                usedTokens: toolsTokens + estimateConversationTokens(conversation),
            });

            let completion;
            try {
                completion = await callDeployment(
                    { endpoint, providerId: deployment.providerId },
                    deployment.model,
                    deployment.apiKey,
                    conversation,
                    toolsEnabled ? openAiTools : [],
                );
            } catch (error) {
                if (error instanceof ToolCallingUnsupportedError && toolsEnabled) {
                    toolsEnabled = false;
                    sseWrite(res, 'warning', {
                        message: 'This Deployment\'s model server was not started with tool-calling support ' +
                            '(vLLM --enable-auto-tool-choice --tool-call-parser). Continuing as a plain chat without Lens MCP tools.',
                    });
                    turn -= 1;
                    continue;
                }
                throw error;
            }
            const message = completion.choices?.[0]?.message;
            if (!message) throw new Error('Deployment returned no message');

            if (message.reasoning_content) {
                sseWrite(res, 'think', { content: message.reasoning_content });
            }

            if (!message.tool_calls?.length) {
                const { think, answer } = splitInlineThink(message.content);
                if (think) sseWrite(res, 'think', { content: think });
                sseWrite(res, 'message', { content: answer ?? '' });
                finalAnswerText = answer ?? '';
                break;
            }

            // The model can (and, per SYSTEM_PROMPT, should) narrate progress
            // in plain text alongside a tool_calls request rather than only
            // speaking once it's completely done -- e.g. "still deploying,
            // 1/3 pods ready" right before it polls again. That inline text
            // would otherwise be silently dropped here (only tool_calls
            // matter to the loop below), so surface it as a distinct
            // 'progress' SSE event: visible to the user immediately, but
            // NOT treated as the final answer, since more tool_calls follow.
            if (message.content) {
                const { think, answer } = splitInlineThink(message.content);
                if (think) sseWrite(res, 'think', { content: think });
                if (answer) sseWrite(res, 'progress', { content: answer });
            }

            conversation.push(message);
            const outcome = await processPendingToolCalls(message.tool_calls, conversation, client, res, readOnly);
            if (outcome === 'paused') {
                pausedForConfirmation = true;
                break outerLoop;
            }
            if (outcome === 'denied') {
                turnStopped = true;
                break outerLoop;
            }
        }

        if (!pausedForConfirmation && turn >= MAX_TOOL_CALL_TURNS) {
            // Don't just dump a raw error on the user -- per the same rule
            // as tool-call failures, the model should describe where it got
            // stuck and what it knows so far instead of the chat silently
            // failing. Force one last no-tools turn so it CANNOT request yet
            // another tool_call (physically impossible with tools disabled)
            // and must instead summarize progress/findings/next steps.
            try {
                conversation.push({
                    role: 'system',
                    content:
                        'You have reached the maximum number of tool-calling steps for this turn. Do not ' +
                        'attempt to call any more tools -- none will be executed. Summarize, for the user: ' +
                        'what you have done so far, what you found, what (if anything) remains unresolved, ' +
                        'and what you recommend they do next.',
                });
                const wrapUp = await callDeployment(
                    { endpoint, providerId: deployment.providerId },
                    deployment.model,
                    deployment.apiKey,
                    conversation,
                    [],
                );
                const wrapUpMessage = wrapUp.choices?.[0]?.message;
                if (wrapUpMessage?.reasoning_content) {
                    sseWrite(res, 'think', { content: wrapUpMessage.reasoning_content });
                }
                const { think, answer } = splitInlineThink(wrapUpMessage?.content);
                if (think) sseWrite(res, 'think', { content: think });
                finalAnswerText = answer ?? wrapUpMessage?.content ?? '';
                sseWrite(res, 'message', { content: finalAnswerText });
            } catch {
                // The wrap-up call itself failed (network/model error) -- fall
                // back to the previous plain error event; there's nothing
                // else usable to show the user.
                sseWrite(res, 'error', { message: 'Reached the maximum number of tool-calling turns without a final answer.' });
            }
        }

        // Now that the model has fully finished answering this question (a
        // real final answer, not paused/erroring/out-of-turns), it's safe to
        // compact history for the NEXT question without interrupting this
        // one. Rebuilds the same plain [user, assistant, user, assistant,
        // ...] shape the client resends every turn (no tool-call
        // bookkeeping) by filtering it back out of `conversation`, so this
        // works identically whether this was a fresh turn or a resumed one.
        if (finalAnswerText !== undefined) {
            const logicalHistory: OpenAiMessage[] = [
                ...conversation.filter((entry) => entry.role === 'user' || (entry.role === 'assistant' && !entry.tool_calls?.length)),
                { role: 'assistant', content: finalAnswerText },
            ];
            const toolsTokensForNextTurn = toolsEnabled ? estimateTokens(JSON.stringify(openAiTools)) : 0;
            const usedTokensBeforeCompaction = toolsTokensForNextTurn + estimateConversationTokens(logicalHistory);
            if (usedTokensBeforeCompaction > contextBudget) {
                sseWrite(res, 'compaction_start', { usedTokens: usedTokensBeforeCompaction, maxModelLen });
                let droppedMessageCount = 0;
                // Never drop the question/answer pair that was just
                // produced, even if that alone is still over budget.
                while (logicalHistory.length > 2 && toolsTokensForNextTurn + estimateConversationTokens(logicalHistory) > contextBudget) {
                    logicalHistory.splice(0, 1);
                    droppedMessageCount += 1;
                }
                const usedTokensAfterCompaction = toolsTokensForNextTurn + estimateConversationTokens(logicalHistory);
                sseWrite(res, 'compaction_end', {
                    droppedMessageCount,
                    usedTokensBefore: usedTokensBeforeCompaction,
                    usedTokensAfter: usedTokensAfterCompaction,
                    maxModelLen,
                });
            }
        }

        sseWrite(res, 'done', {});
    } catch (error) {
        sseWrite(res, 'error', { message: error instanceof Error ? error.message : String(error) });
    } finally {
        try {
            await client?.close();
        } catch {
            // ignore
        }
        res.end();
    }
});

// Exposed for tests / diagnostics: the tool names currently registered.
export const registeredToolNames = prismTools.map((tool) => tool.name);
