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

// "Chat with Lens" (docs/design/mcp-playground-design.zh-CN.md, section 6): the
// user first picks a Cluster, then a Deployment on that cluster as the
// conversation's model service, then chats with it. The model calls Prism
// MCP tools (server/mcp); every tool call is shown in a timeline for
// transparency, and any tool the server did not execute (write/approve-tier,
// Phase 2/3) renders as "blocked". Cluster + Deployment selection lives in
// the right-hand rail so the conversation itself stays the focus.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Bot, ChevronRight, Lightbulb, Loader2, Mic, MessageSquarePlus, RefreshCw, ShieldAlert, Square, Wrench, XCircle } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { Button } from '../ui/Button';
import { listClusters, openClusterSession, streamPlaygroundChat } from './playgroundBackend';
import { listDeploymentExecutions } from '../OptimizationWorkspace/remoteDeployBackend';
import { listAIProviders } from '../AIProviders/aiProvidersBackend';
import { getMicUnavailableReason, isSpeechToTextSupported, SPEECH_TO_TEXT_LANGUAGES, transcribeAudioBlob } from './speechToText';

// A playful variant of lucide's Bot icon: the eyes (the two short vertical
// bars) occasionally blink, and the whole icon occasionally hops in place.
// Pure CSS animation (see .prism-bot-icon / .prism-bot-eyes in index.css) so
// it costs nothing beyond the header render.
function AnimatedBotIcon({ className }) {
    return (
        <svg
            className={`prism-bot-icon ${className || ''}`}
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
        >
            <path d="M12 8V4H8" />
            <rect width="16" height="12" x="4" y="8" rx="2" />
            <path d="M2 14h2" />
            <path d="M20 14h2" />
            <g className="prism-bot-eyes">
                <path d="M15 13v2" />
                <path d="M9 13v2" />
            </g>
        </svg>
    );
}

// A larger, rotating pool of prompts shown as floating speech bubbles near
// the bot icon — more variety than the fixed chip row since these take turns
// rather than all being visible at once.
const BUBBLE_SUGGESTED_QUESTIONS = [
    'Do I have any ready deployments?',
    'What does my current cluster stack status look like?',
    'Are there any pending benchmarks or evaluations?',
    'What agentic plans are currently running?',
    'Which clusters are unhealthy right now?',
    'Show me the deployments running on this cluster.',
    'What models are currently deployed?',
    'Summarize my recent benchmark results.',
    'Is my last deployment status healthy or failed?',
    'Which deploy validation checks failed, if any?',
    'What guide combinations can Prism deploy for me?',
    'Show me the agentic plan recommendations.',
    'What does the flow map look like for my traffic?',
    'How many deployments are ready across all clusters?',
    'What accelerators are available in the guide catalog?',
    'Any anomalies in my current flow map metrics?',
];

// Seconds each suggested-question bubble stays up before the next one takes
// its turn; keep in sync with the ~6% hold-time window in the
// .prism-bubble keyframes (index.css) — those percentages are of
// BUBBLE_SUGGESTED_QUESTIONS.length * BUBBLE_INTERVAL_SECONDS.
const BUBBLE_INTERVAL_SECONDS = 4.5;

// Scatters bubbles around the bot icon at random angles/distances, close to
// it but never overlapping: the circle is split into one angular slice per
// bubble (guaranteeing separation) and each bubble gets a random angle
// within its own slice plus a randomized radius, so the layout looks
// organic rather than in fixed slots. Computed once per mount via useMemo in
// the component (not on every render, so bubbles don't jump around).
function randomBubblePlacements(count) {
    const sliceDeg = 360 / count;
    const minRadiusRem = 8.5; // measured to bubble center, so its near edge still clears the icon face
    const maxRadiusRem = 10;
    const startAngle = Math.random() * 360;
    return Array.from({ length: count }, (_, index) => {
        // Keep to the middle 70% of each slice so neighboring bubbles keep a
        // guaranteed angular gap between them.
        const angle = startAngle + index * sliceDeg + sliceDeg * (0.15 + Math.random() * 0.7);
        const radius = minRadiusRem + Math.random() * (maxRadiusRem - minRadiusRem);
        const radians = (angle * Math.PI) / 180;
        return {
            top: `calc(50% + ${(Math.sin(radians) * radius).toFixed(2)}rem)`,
            left: `calc(50% + ${(Math.cos(radians) * radius).toFixed(2)}rem)`,
        };
    });
}

// Tiny (12px) conic-gradient pie showing how much of the model's context
// window the current conversation is using -- color shifts from blue to
// amber to red as it approaches the limit, matching the trim/drop warnings
// the backend emits at the same thresholds.
function ContextUsagePie({ ratio }) {
    const clamped = Math.max(0, Math.min(1, ratio || 0));
    const percent = Math.round(clamped * 100);
    const color = clamped >= 0.95 ? '#f87171' : clamped >= 0.8 ? '#fbbf24' : '#38bdf8';
    return (
        <div
            className="h-3 w-3 shrink-0 rounded-full"
            style={{ background: `conic-gradient(${color} ${percent}%, #334155 0)` }}
        />
    );
}

// Renders a model's chain-of-thought, collapsed by default: only the
// "Thinking" summary label is visible until the user expands it. Deliberately
// unframed (no border/box), matching the plain tool-call rows below it.
function ThinkBlock({ content, pending }) {
    return (
        <details className="group px-1 text-xs text-slate-400">
            <summary className="flex cursor-pointer list-none items-center gap-1.5 font-sans font-medium text-slate-400 marker:content-none">
                <ChevronRight className="h-3.5 w-3.5 shrink-0 transition-transform group-open:rotate-90" />
                <Lightbulb className="h-3.5 w-3.5 shrink-0" />
                <span>Thinking{pending && <Loader2 className="ml-1 inline h-3 w-3 animate-spin align-middle" />}</span>
            </summary>
            <pre className="mt-1 whitespace-pre-wrap break-words pl-5 text-[11px] italic text-slate-500">
                {content}
            </pre>
        </details>
    );
}

// One MCP tool call, collapsed by default: the summary line shows only which
// tool was called (and whether it was blocked), while arguments/result stay
// hidden until expanded. Deliberately unframed (no border/box) — just an
// indented, muted line matching the think block above it. A small red X
// appears in front of the summary when the call errored, so a failure is
// visible at a glance without expanding every entry.
function ToolCallEntry({ entry, onResolve }) {
    if (entry.kind === 'confirm') {
        return <ConfirmToolEntry entry={entry} onResolve={onResolve} />;
    }
    if (entry.kind === 'permission_denied') {
        // Authorization is final: the assistant stopped the turn instead of
        // retrying. Surface it as a plain, non-retryable notice.
        return (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-2 py-1.5 text-xs text-amber-200">
                <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <div className="min-w-0">
                    <p className="font-sans font-semibold">Permission required</p>
                    <p className="mt-0.5 font-sans normal-case">{entry.message}</p>
                </div>
            </div>
        );
    }
    if (entry.kind === 'progress') {
        // An interim status update the model narrated alongside a tool
        // call (e.g. "still starting up, 1/3 pods ready") -- rendered
        // plainly and always visible, unlike the collapsed think/tool
        // entries, since the whole point is for the user to see it without
        // having to expand anything.
        return (
            <div className="flex items-start gap-1.5 px-1 py-0.5 text-xs text-sky-300">
                <Bot className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{entry.content}</span>
            </div>
        );
    }
    const isBlocked = entry.kind === 'blocked';
    return (
        <details className={'group px-1 text-xs font-mono ' + (isBlocked ? 'text-amber-300' : 'text-slate-400')}>
            <summary className="flex cursor-pointer list-none items-center gap-1.5 font-sans font-medium marker:content-none">
                <ChevronRight className="h-3.5 w-3.5 shrink-0 transition-transform group-open:rotate-90" />
                {entry.isError && <XCircle className="h-3.5 w-3.5 shrink-0 text-rose-500" />}
                {isBlocked ? <ShieldAlert className="h-3.5 w-3.5 shrink-0" /> : <Wrench className="h-3.5 w-3.5 shrink-0" />}
                <span>{isBlocked ? 'Blocked (unknown tool)' : 'Tool call'}</span>
                <span className="text-slate-500">{entry.name}</span>
            </summary>
            <pre className="mt-1 whitespace-pre-wrap break-all pl-5 text-[11px] text-slate-500">
                {JSON.stringify(entry.arguments, null, 2)}
            </pre>
            {entry.result !== undefined && (
                <pre className={'mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all pl-5 text-[11px] ' + (entry.isError ? 'text-rose-400/80' : 'text-emerald-400/80')}>
                    {JSON.stringify(entry.result, null, 2)}
                </pre>
            )}
        </details>
    );
}

// An approve-tier tool call the model wants to run, paused until the user
// decides. Only approve-tier tools ever reach here now -- write-tier ones
// run straight through like read-tier ones (see chat.ts) -- so this is
// always rendered "loud": red-flagged and gated behind an explicit "I
// reviewed this" checkbox, since it covers riskier/harder-to-undo actions.
// Kept generic on `entry.riskTier` rather than hardcoded to "approve" in
// case a lighter write-tier confirmation is ever reintroduced. Expanded
// while a decision is still pending, and collapses automatically as soon
// as the user approves/rejects, so a resolved confirmation doesn't keep
// taking up space in the transcript.
function ConfirmToolEntry({ entry, onResolve }) {
    const isApproveTier = entry.riskTier === 'approve';
    const [acknowledged, setAcknowledged] = useState(false);
    const decided = entry.decision != null;
    const resolved = entry.result !== undefined;

    let statusLabel = null;
    if (decided && !resolved) {
        statusLabel = entry.decision === 'approve' ? 'Approving…' : 'Rejecting…';
    } else if (resolved) {
        statusLabel = entry.result?.rejected ? 'Rejected' : 'Approved';
    }

    return (
        <details
            open={!decided}
            className={
                'group rounded-md px-2 py-1.5 text-xs font-mono ' +
                (isApproveTier ? 'border border-rose-500/40 bg-rose-500/5 text-rose-200' : 'border border-amber-500/30 bg-amber-500/5 text-amber-200')
            }
        >
            <summary className="flex cursor-pointer list-none items-center gap-1.5 font-sans font-medium marker:content-none">
                <ChevronRight className="h-3.5 w-3.5 shrink-0 transition-transform group-open:rotate-90" />
                <ShieldAlert className="h-3.5 w-3.5 shrink-0" />
                <span>{isApproveTier ? 'Needs approval' : 'Needs confirmation'}</span>
                <span className="text-slate-400">{entry.name}</span>
                {statusLabel && <span className="ml-auto font-sans text-[11px] font-normal text-slate-400">{statusLabel}</span>}
            </summary>
            {entry.description && <p className="mt-1 pl-5 font-sans text-[11px] normal-case text-slate-400">{entry.description}</p>}
            <pre className="mt-1 whitespace-pre-wrap break-all pl-5 text-[11px] text-slate-400">
                {JSON.stringify(entry.arguments, null, 2)}
            </pre>
            {!decided && (
                <div className="mt-2 flex flex-col gap-2 pl-5">
                    {isApproveTier && (
                        <label className="flex items-center gap-1.5 font-sans text-[11px] normal-case text-slate-300">
                            <input
                                type="checkbox"
                                checked={acknowledged}
                                onChange={(event) => setAcknowledged(event.target.checked)}
                                className="h-3.5 w-3.5 rounded border-slate-500"
                            />
                            I reviewed the arguments above and want to proceed.
                        </label>
                    )}
                    <div className="flex gap-2">
                        <button
                            type="button"
                            onClick={() => onResolve(entry.id, 'approve')}
                            disabled={isApproveTier && !acknowledged}
                            className="rounded-md bg-emerald-600/80 px-2.5 py-1 font-sans text-[11px] font-medium text-white hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-40"
                        >
                            Approve
                        </button>
                        <button
                            type="button"
                            onClick={() => onResolve(entry.id, 'reject')}
                            className="rounded-md bg-slate-700 px-2.5 py-1 font-sans text-[11px] font-medium text-slate-200 hover:bg-slate-600"
                        >
                            Reject
                        </button>
                    </div>
                </div>
            )}
            {resolved && (
                <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all pl-5 text-[11px] text-emerald-400/80">
                    {JSON.stringify(entry.result, null, 2)}
                </pre>
            )}
        </details>
    );
}

function ChatMessage({ message, onResolveConfirm }) {
    // Think + tool-calls are rendered as ONE block with its own tight
    // internal gap, instead of two separate top-level list items whose
    // spacing depends on the parent list's margin utility. This keeps the
    // "Thinking" and "Tool call" rows visually right next to each other
    // regardless of how the surrounding space-y-* gap is implemented.
    if (message.role === 'reasoning') {
        return (
            <div className="flex flex-col gap-0.5 pl-9">
                {message.think != null && <ThinkBlock content={message.think} pending={message.pending} />}
                {message.entries?.map((entry) => (
                    <ToolCallEntry key={entry.id} entry={entry} onResolve={onResolveConfirm} />
                ))}
            </div>
        );
    }

    if (message.role === 'system-note') {
        return (
            <div className="flex items-center justify-center gap-1.5 py-1 text-[11px] text-slate-500">
                {message.pending && <Loader2 className="h-3 w-3 shrink-0 animate-spin" />}
                <span>{message.pending ? 'Compacting conversation to fit the context window…' : message.content}</span>
            </div>
        );
    }

    const isUser = message.role === 'user';
    return (
        <div className={'flex gap-2 ' + (isUser ? 'flex-row-reverse text-right' : '')}>
            <div
                className={
                    'flex h-7 w-7 shrink-0 items-center justify-center rounded-full ' +
                    (isUser ? 'bg-sky-500/20 text-sky-300' : 'bg-emerald-500/20 text-emerald-300')
                }
            >
                {isUser ? 'U' : <Bot className="h-4 w-4" />}
            </div>
            <div
                className={
                    'max-w-2xl rounded-xl px-3.5 py-2 text-sm ' +
                    (isUser ? 'whitespace-pre-wrap bg-sky-500/10 text-slate-100' : 'markdown-body bg-slate-800/70 text-slate-100')
                }
            >
                {isUser ? (
                    message.content
                ) : (
                    // react-markdown (+ remark-gfm for tables/strikethrough/task
                    // lists) is a mature, widely-used CommonMark renderer --
                    // swapped in for the earlier hand-rolled renderer, which
                    // looked noticeably rougher. Still styled via the existing
                    // .markdown-body CSS (src/index.css) for visual consistency
                    // with the HuggingFace README viewer.
                    <div className="markdown-body">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content || ''}</ReactMarkdown>
                    </div>
                )}
                {message.pending && <Loader2 className="ml-1 inline h-3.5 w-3.5 animate-spin align-middle" />}
            </div>
        </div>
    );
}

// Module-level store (deliberately outside React state/component lifecycle)
// holding the Lens Assistant conversation so it survives navigating away to
// another page and back: App.jsx only conditionally renders PlaygroundPage
// (`currentView === 'playground' && <PlaygroundPage .../>`), so it fully
// unmounts on every view switch and would otherwise lose all local state.
// This plain module-scoped object persists for the lifetime of the page (SPA
// navigation, not a hard refresh) and is only ever reset by an explicit "New
// chat" click (see handleNewChat below) -- never by mount/unmount. Holds
// exactly the fields that make up "the conversation" (what's said, which
// provider/deployment it's with, and context-window usage); ephemeral
// in-flight UI state (input draft, sending/compacting flags, pending
// confirmations, errors) is intentionally NOT persisted here, since it's
// either meaningless after a real navigation-triggered unmount (an in-flight
// stream is aborted on unmount anyway) or fine to just reset.
function createDefaultChatSession() {
    return {
        providerMode: 'external',
        selectedClusterId: '',
        clusterSessionId: '',
        selectedId: '',
        selectedProviderId: '',
        messages: [],
        maxModelLen: null,
        usedTokens: 0,
    };
}
let chatSession = createDefaultChatSession();

export default function PlaygroundPage({ onToggleMobileNav }) {
    // "AI provider" mode: Internal (pick a Cluster, then a Deployment already
    // running on it) or External (pick a saved External AI provider — see
    // Resources → External providers). Only one is active at a time.
    const [providerMode, setProviderMode] = useState(() => chatSession.providerMode);

    const [clusters, setClusters] = useState([]);
    const [loadingClusters, setLoadingClusters] = useState(true);
    const [clustersError, setClustersError] = useState('');
    const [selectedClusterId, setSelectedClusterId] = useState(() => chatSession.selectedClusterId);
    const [clusterSessionId, setClusterSessionId] = useState(() => chatSession.clusterSessionId);
    const [resolvingSession, setResolvingSession] = useState(false);

    const [deployments, setDeployments] = useState([]);
    const [loadingDeployments, setLoadingDeployments] = useState(false);
    const [deploymentsError, setDeploymentsError] = useState('');
    const [selectedId, setSelectedId] = useState(() => chatSession.selectedId);

    const [aiProviders, setAiProviders] = useState([]);
    const [loadingAiProviders, setLoadingAiProviders] = useState(true);
    const [aiProvidersError, setAiProvidersError] = useState('');
    const [selectedProviderId, setSelectedProviderId] = useState(() => chatSession.selectedProviderId);

    const [messages, setMessages] = useState(() => chatSession.messages);
    const [input, setInput] = useState('');
    const [sending, setSending] = useState(false);
    const [chatError, setChatError] = useState('');
    const abortRef = useRef(null);
    // Resume data for tool calls currently paused on a `confirm_required`
    // event, keyed by tool_call id: the exact resume conversation blob plus
    // the shared thinkChunks/toolEntries arrays for that turn, so approving
    // or rejecting continues updating the SAME reasoning block instead of
    // starting a new one. `pendingConfirmCount` mirrors its size in state
    // purely to re-render (e.g. disable sending a new message) when it
    // changes -- the ref itself doesn't trigger renders.
    const pendingConfirmationsRef = useRef(new Map());
    const [pendingConfirmCount, setPendingConfirmCount] = useState(0);
    const chatScrollRef = useRef(null);

    // Context-window usage for the small number + pie indicator next to the
    // input box: maxModelLen comes from the backend's best-effort discovery
    // (see discoverMaxModelLen in server/playground/chat.ts), usedTokens is
    // this turn's post-truncation/post-drop estimated prompt size.
    const [maxModelLen, setMaxModelLen] = useState(() => chatSession.maxModelLen);
    const [usedTokens, setUsedTokens] = useState(() => chatSession.usedTokens);
    // True only while the backend is compacting (dropping older history to
    // fit the context window) between turns -- always AFTER the model has
    // fully answered the current question, never mid-answer, so it can't
    // interrupt an in-progress response. Freezes input meanwhile since the
    // history the next message would be built on is actively changing.
    const [compacting, setCompacting] = useState(false);

    // Voice input (speechToText.js): fully client-side, no backend involved.
    const [recording, setRecording] = useState(false);
    const [transcribing, setTranscribing] = useState(false);
    const [micError, setMicError] = useState('');
    const [micLanguage, setMicLanguage] = useState(SPEECH_TO_TEXT_LANGUAGES[0].value);
    const mediaRecorderRef = useRef(null);
    const audioChunksRef = useRef([]);

    // Computed once so bubbles don't re-scatter to new random spots on every
    // render (e.g. while typing).
    const bubblePlacements = useMemo(() => randomBubblePlacements(BUBBLE_SUGGESTED_QUESTIONS.length), []);

    const loadClusters = useCallback(async () => {
        setLoadingClusters(true);
        setClustersError('');
        try {
            const items = await listClusters();
            setClusters(items);
        } catch (failure) {
            setClustersError(failure instanceof Error ? failure.message : 'Failed to load clusters');
        } finally {
            setLoadingClusters(false);
        }
    }, []);

    useEffect(() => {
        loadClusters();
    }, [loadClusters]);

    // The chat orchestrator's OpenAI-compatible passthrough (proxy_chat_completions
    // in llm_d_bench/ai_providers/router.py) only supports "openai"-type saved
    // providers today (Anthropic's wire format differs and isn't translated),
    // so only those are offered here.
    const loadAiProviders = useCallback(async () => {
        setLoadingAiProviders(true);
        setAiProvidersError('');
        try {
            const items = await listAIProviders();
            const openAiItems = items.filter((provider) => provider.providerType === 'openai');
            setAiProviders(openAiItems);
            setSelectedProviderId((current) => (current && openAiItems.some((provider) => provider.id === current) ? current : openAiItems[0]?.id || ''));
        } catch (failure) {
            setAiProvidersError(failure instanceof Error ? failure.message : 'Failed to load External AI providers');
        } finally {
            setLoadingAiProviders(false);
        }
    }, []);

    useEffect(() => {
        loadAiProviders();
    }, [loadAiProviders]);

    const loadDeployments = useCallback(async (clusterId) => {
        setLoadingDeployments(true);
        setDeploymentsError('');
        try {
            // Straight from the same GET /api/v1/deployments/executions the
            // Optimization Deployments page uses — no bespoke Playground API.
            const items = await listDeploymentExecutions({ status: 'ready', clusterId });
            const mapped = items.map((item) => ({
                id: item.execution_id,
                namespace: item.namespace,
                model: item.model,
                ready: item.status === 'ready',
                gatewayEndpoint: item.endpoint || item.forwarded_endpoint || null,
            }));
            const ready = mapped.filter((deployment) => deployment.ready && deployment.gatewayEndpoint);
            setDeployments(mapped);
            setSelectedId((current) => (current && mapped.some((d) => d.id === current) ? current : ready[0]?.id || ''));
        } catch (failure) {
            setDeploymentsError(failure instanceof Error ? failure.message : 'Failed to load deployments');
        } finally {
            setLoadingDeployments(false);
        }
    }, []);

    // Re-hydrates the `deployments` list on mount when a Cluster selection
    // was restored from chatSession (see below): `deployments` itself is
    // never persisted (it's just a live listing, cheap to refetch), but
    // normally only handleSelectCluster's interactive flow loads it, so
    // without this a restored selectedClusterId/selectedId would point at
    // an empty list after navigating back to this page. loadDeployments
    // already preserves selectedId if it's still present in the refreshed
    // list, so the previously-selected Deployment naturally survives. Runs
    // once per mount only -- deliberately not re-run on every
    // selectedClusterId change, since handleSelectCluster already owns that.
    const hydratedDeploymentsRef = useRef(false);
    useEffect(() => {
        if (hydratedDeploymentsRef.current) return;
        hydratedDeploymentsRef.current = true;
        if (selectedClusterId) loadDeployments(selectedClusterId);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Keeps the module-level chatSession store (see createDefaultChatSession
    // above) in sync with the conversation-related state so it survives this
    // component unmounting when the user navigates to another page.
    useEffect(() => {
        chatSession = {
            providerMode,
            selectedClusterId,
            clusterSessionId,
            selectedId,
            selectedProviderId,
            messages,
            maxModelLen,
            usedTokens,
        };
    }, [providerMode, selectedClusterId, clusterSessionId, selectedId, selectedProviderId, messages, maxModelLen, usedTokens]);

    // "New chat": clears only the conversation itself (messages, context-window
    // usage, any paused confirmation, in-flight errors) -- the current AI
    // provider/Cluster/Deployment selection is left alone, matching the
    // convention chat UIs generally use (a fresh conversation with the same
    // model, not a full settings reset). Aborts any in-flight turn first so
    // a stray response can't land in the fresh, empty conversation right
    // after.
    const handleNewChat = useCallback(() => {
        abortRef.current?.abort();
        pendingConfirmationsRef.current.clear();
        setPendingConfirmCount(0);
        setMessages([]);
        setInput('');
        setChatError('');
        setCompacting(false);
        setSending(false);
        setMaxModelLen(null);
        setUsedTokens(0);
    }, []);

    // Selecting a Deployment is gated on a Cluster being selected first: pick
    // a Cluster -> resolve/open its session -> only then load Deployments.
    const handleSelectCluster = useCallback(
        async (clusterId) => {
            setSelectedClusterId(clusterId);
            setClusterSessionId('');
            setDeployments([]);
            setSelectedId('');
            setDeploymentsError('');
            if (!clusterId) return;
            setResolvingSession(true);
            try {
                const sessionId = await openClusterSession(clusterId);
                setClusterSessionId(sessionId);
                await loadDeployments(clusterId);
            } catch (failure) {
                setDeploymentsError(failure instanceof Error ? failure.message : 'Failed to open cluster session');
            } finally {
                setResolvingSession(false);
            }
        },
        [loadDeployments],
    );

    // Auto-pick the first ready Cluster so the user isn't forced to make a
    // choice when there's an obvious default; they can still switch later.
    useEffect(() => {
        if (selectedClusterId || loadingClusters || clusters.length === 0) return;
        const firstReady = clusters.find((cluster) => cluster.ready);
        if (firstReady) handleSelectCluster(firstReady.id);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [clusters, loadingClusters]);

    const selectedCluster = clusters.find((cluster) => cluster.id === selectedClusterId) || null;
    const readyDeployments = deployments.filter((deployment) => deployment.ready && deployment.gatewayEndpoint);
    const selectedDeployment = deployments.find((deployment) => deployment.id === selectedId) || null;
    const selectedProvider = aiProviders.find((provider) => provider.id === selectedProviderId) || null;

    // Keep the chat log pinned to the newest message: scrolls to the bottom
    // whenever the conversation content changes (new message, streaming
    // tokens, tool-call interstitials, etc.), so the user never has to
    // manually scroll down to see the latest reply.
    useEffect(() => {
        const container = chatScrollRef.current;
        if (!container) return;
        container.scrollTop = container.scrollHeight;
    }, [messages]);

    // Unifies the two AI provider modes into one shape the chat/rendering
    // code below can use without branching everywhere: a label to show in
    // the "Talking to ..." banner, the model name, and the deployment
    // payload to send to POST /api/playground/chat.
    const activeService = useMemo(() => (
        providerMode === 'internal'
            ? (selectedDeployment && {
                label: selectedDeployment.namespace,
                model: selectedDeployment.model || 'default',
                deploymentPayload: {
                    executionId: selectedDeployment.id,
                    model: selectedDeployment.model || 'default',
                },
            })
            : (selectedProvider && {
                label: selectedProvider.name,
                model: selectedProvider.model || 'default',
                deploymentPayload: {
                    providerId: selectedProvider.id,
                    model: selectedProvider.model || 'default',
                },
            })
    ), [providerMode, selectedDeployment, selectedProvider]);

    // Drives one SSE turn (a fresh message, or resuming one paused on a
    // confirm_required event) against a shared thinkChunks/toolEntries pair,
    // so both call sites funnel through identical event handling.
    const driveChatStream = useCallback(async (requestBody, thinkChunks, toolEntries) => {
        setChatError('');
        setSending(true);
        const controller = new AbortController();
        abortRef.current = controller;

        try {
            await streamPlaygroundChat(
                requestBody,
                (type, data) => {
                    if (type === 'think') {
                        thinkChunks.push(data.content);
                        setMessages((prev) => withInterstitial(prev, thinkChunks, toolEntries));
                    } else if (type === 'tool_call') {
                        toolEntries.push({ id: data.id, kind: 'tool_call', name: data.name, arguments: data.arguments });
                        setMessages((prev) => withInterstitial(prev, thinkChunks, toolEntries));
                    } else if (type === 'tool_result') {
                        const entry = toolEntries.find((item) => item.id === data.id);
                        if (entry) {
                            entry.result = data.result;
                            entry.isError = Boolean(data.isError || data.result?.isError || data.result?.error);
                        }
                        setMessages((prev) => withInterstitial(prev, thinkChunks, toolEntries));
                    } else if (type === 'blocked') {
                        toolEntries.push({ id: `${data.name}-${toolEntries.length}`, kind: 'blocked', name: data.name, arguments: data.arguments });
                        setMessages((prev) => withInterstitial(prev, thinkChunks, toolEntries));
                    } else if (type === 'permission_denied') {
                        // The assistant stopped the turn on a 401/403 instead of
                        // retrying; show a clear, non-retryable notice and end
                        // the turn (no final answer bubble, no spinner).
                        toolEntries.push({
                            id: `permission-${data.id || toolEntries.length}`,
                            kind: 'permission_denied',
                            name: data.name,
                            message: data.message,
                            requiredPermission: data.requiredPermission,
                        });
                        setMessages((prev) => finalizePendingTurn(withInterstitial(prev, thinkChunks, toolEntries)));
                    } else if (type === 'progress') {
                        // Interim narration the model produced alongside a
                        // tool call (see chat.ts) -- surface it right away
                        // as its own entry rather than waiting for the
                        // final 'message' event, so the user sees periodic
                        // updates while a long-running operation (like a
                        // deployment starting up) is still in progress.
                        if (data.content) {
                            toolEntries.push({ id: `progress-${toolEntries.length}`, kind: 'progress', content: data.content });
                            setMessages((prev) => withInterstitial(prev, thinkChunks, toolEntries));
                        }
                    } else if (type === 'context_usage') {
                        setMaxModelLen(data.maxModelLen ?? null);
                        setUsedTokens(data.usedTokens ?? 0);
                    } else if (type === 'compaction_start') {
                        // Only ever fires after the model has fully answered
                        // the current question (see chat.ts) -- freeze the
                        // input and show a status note while older history
                        // is being dropped for the NEXT question.
                        setCompacting(true);
                        setMessages((prev) => [...prev, { role: 'system-note', pending: true }]);
                    } else if (type === 'compaction_end') {
                        setCompacting(false);
                        const droppedCount = data.droppedMessageCount ?? 0;
                        const noteText = droppedCount > 0
                            ? `Compacted: removed ${droppedCount} earlier message${droppedCount === 1 ? '' : 's'} to fit the model's context window.`
                            : 'Context window is nearly full and could not be reduced further — consider starting a new chat.';
                        setMessages((prev) => {
                            const withoutPendingNote = prev.filter((message) => !(message.role === 'system-note' && message.pending));
                            const compacted = applyHistoryCompaction(withoutPendingNote, droppedCount);
                            return [{ role: 'system-note', content: noteText }, ...compacted];
                        });
                        setMaxModelLen(data.maxModelLen ?? null);
                        setUsedTokens(data.usedTokensAfter ?? 0);
                    } else if (type === 'confirm_required') {
                        // The model wants to run an approve-tier tool.
                        // Show it as an interactive entry instead of
                        // auto-running it, and stash everything needed to
                        // resume this exact turn once the user decides.
                        toolEntries.push({
                            id: data.id,
                            kind: 'confirm',
                            name: data.name,
                            arguments: data.arguments,
                            riskTier: data.riskTier,
                            description: data.description,
                        });
                        pendingConfirmationsRef.current.set(data.id, {
                            resumeConversation: data.resumeConversation,
                            deployment: requestBody.deployment,
                            thinkChunks,
                            toolEntries,
                        });
                        setPendingConfirmCount(pendingConfirmationsRef.current.size);
                        setMessages((prev) => {
                            const withEntries = withInterstitial(prev, thinkChunks, toolEntries);
                            // No final answer this turn -- the model is
                            // paused on the confirmation above, so drop the
                            // empty pending assistant bubble instead of
                            // leaving it stuck on a spinner.
                            return withEntries.filter((message) => !(message.role === 'assistant' && message.pending));
                        });
                    } else if (type === 'message') {
                        // Defense in depth: strip any <think>...</think> the
                        // backend might not have caught, so it never leaks
                        // into the visible answer bubble.
                        const { think, answer } = splitInlineThink(data.content);
                        if (think) {
                            thinkChunks.push(think);
                            setMessages((prev) => withInterstitial(prev, thinkChunks, toolEntries));
                        }
                        setMessages((prev) => replacePendingAssistant(prev, answer));
                    } else if (type === 'error') {
                        setChatError(data.message || 'Playground chat failed');
                        setMessages((prev) => replacePendingAssistant(prev, null));
                    } else if (type === 'done') {
                        // Safety net: a turn that already produced a message is
                        // unaffected, but one that ended with a terminal outcome
                        // (e.g. an authorization stop) must not leave a spinner.
                        setMessages((prev) => finalizePendingTurn(prev));
                    }
                },
                controller.signal,
            );
        } catch (failure) {
            setChatError(failure instanceof Error ? failure.message : 'Playground chat failed');
            setMessages((prev) => replacePendingAssistant(prev, null));
        } finally {
            setSending(false);
            abortRef.current = null;
        }
    }, []);

    const sendMessage = useCallback(async (overrideText, { readOnly = false } = {}) => {
        const text = (overrideText ?? input).trim();
        if (!text || !activeService || sending || pendingConfirmCount > 0 || compacting) return;
        setInput('');

        const history = [...messages, { role: 'user', content: text }];
        setMessages((prev) => [...prev, { role: 'user', content: text }, { role: 'assistant', content: '', pending: true }]);

        const openAiMessages = history
            .filter((message) => message.role === 'user' || message.role === 'assistant')
            .map((message) => ({ role: message.role, content: message.content }));

        // Voice-originated messages are transcribed by an on-device model
        // the user can't fully verify before it's sent -- restrict this
        // turn to read-only Lens MCP tools so a misheard word can never
        // even reach a write/approve-tier tool, confirmation dialog or not.
        await driveChatStream({ deployment: activeService.deploymentPayload, messages: openAiMessages, readOnly }, [], []);
    }, [input, messages, activeService, sending, pendingConfirmCount, compacting, driveChatStream]);

    // Approve or reject a paused approve-tier tool call, resuming the
    // exact turn it paused (same reasoning block, same underlying
    // conversation) instead of starting a fresh one.
    const resolveConfirmation = useCallback(async (id, decision) => {
        const pending = pendingConfirmationsRef.current.get(id);
        if (!pending) return;
        pendingConfirmationsRef.current.delete(id);
        setPendingConfirmCount(pendingConfirmationsRef.current.size);

        // Reflect the decision immediately so the Approve/Reject buttons
        // disappear right away, before the network round-trip resolves.
        const entry = pending.toolEntries.find((item) => item.id === id);
        if (entry) entry.decision = decision;
        setMessages((prev) => withInterstitial(prev, pending.thinkChunks, pending.toolEntries));

        await driveChatStream(
            {
                deployment: pending.deployment,
                pendingResume: { conversation: pending.resumeConversation, toolCallId: id, decision },
            },
            pending.thinkChunks,
            pending.toolEntries,
        );
    }, [driveChatStream]);

    // Starts recording from the mic; the actual transcription happens once
    // recording stops (see the recorder's onstop handler below) so the whole
    // utterance is transcribed in one pass instead of streaming partial audio.
    const startRecording = useCallback(async () => {
        setMicError('');
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            const recorder = new MediaRecorder(stream);
            audioChunksRef.current = [];
            recorder.ondataavailable = (event) => {
                if (event.data.size > 0) audioChunksRef.current.push(event.data);
            };
            recorder.onstop = async () => {
                stream.getTracks().forEach((track) => track.stop());
                const blob = new Blob(audioChunksRef.current, { type: recorder.mimeType || 'audio/webm' });
                setTranscribing(true);
                try {
                    const text = await transcribeAudioBlob(blob, { language: micLanguage });
                    if (text) sendMessage(text, { readOnly: true });
                } catch (failure) {
                    setMicError(failure instanceof Error ? failure.message : 'Speech-to-text failed');
                } finally {
                    setTranscribing(false);
                }
            };
            recorder.start();
            mediaRecorderRef.current = recorder;
            setRecording(true);
        } catch (failure) {
            setMicError(failure instanceof Error ? failure.message : 'Microphone access was denied or is unavailable');
        }
    }, [micLanguage, sendMessage]);

    const stopRecording = useCallback(() => {
        mediaRecorderRef.current?.stop();
        setRecording(false);
    }, []);

    return (
        <ModulePage className="h-screen overflow-hidden" contentClassName="flex h-full min-h-0 flex-col">
            <ModuleHeader
                icon={AnimatedBotIcon}
                title="Lens Assistant"
                badge="Experimental"
                description="Ask a Lens AI assistant to manage your llm-d clusters."
                onToggleMobileNav={onToggleMobileNav}
                className="shrink-0"
                actions={
                    <Button size="sm" variant="sky" onClick={handleNewChat} disabled={messages.length === 0 && !chatError}>
                        <MessageSquarePlus size={14} /> New chat
                    </Button>
                }
            />

            <div className="mt-4 flex min-h-0 flex-1 flex-col gap-4 lg:flex-row">
                <div className="flex min-h-0 flex-1 flex-col rounded-xl border border-slate-800 bg-slate-900/30">
                    <div
                        ref={chatScrollRef}
                        className={
                            messages.length === 0
                                ? 'flex min-h-0 flex-1 items-center justify-center overflow-y-auto p-4'
                                : 'min-h-0 flex-1 space-y-3 overflow-y-auto p-4'
                        }
                    >
                        {messages.length === 0 ? (
                            <div className="flex flex-col items-center gap-2 text-center">
                                <div className="relative mb-2 flex h-24 w-24 items-center justify-center rounded-full border border-cyan-500/30 bg-cyan-500/10 text-cyan-400">
                                    <AnimatedBotIcon className="h-12 w-12" />
                                    {BUBBLE_SUGGESTED_QUESTIONS.map((question, index) => (
                                        <button
                                            key={question}
                                            type="button"
                                            disabled={!activeService || sending || pendingConfirmCount > 0 || compacting}
                                            onClick={() => sendMessage(question)}
                                            style={{
                                                animationDelay: `${index * BUBBLE_INTERVAL_SECONDS}s`,
                                                animationDuration: `${BUBBLE_SUGGESTED_QUESTIONS.length * BUBBLE_INTERVAL_SECONDS}s`,
                                                translate: '-50% -50%',
                                                ...bubblePlacements[index],
                                            }}
                                            className="prism-bubble absolute z-10 w-max max-w-[11rem] whitespace-normal rounded-md bg-[#95ec69] px-3 py-1.5 text-left text-xs font-medium text-slate-900 shadow-lg shadow-black/30 transition-colors hover:bg-[#86e058] disabled:cursor-not-allowed disabled:opacity-50"
                                        >
                                            {question}
                                        </button>
                                    ))}
                                </div>
                            </div>
                        ) : (
                            messages.map((message, index) => (
                                <div key={index}>
                                    <ChatMessage message={message} onResolveConfirm={resolveConfirmation} />
                                </div>
                            ))
                        )}
                    </div>
                    {chatError && <p className="border-t border-slate-800 px-4 py-2 text-xs text-red-400">{chatError}</p>}
                    {micError && <p className="border-t border-slate-800 px-4 py-2 text-xs text-red-400">{micError}</p>}
                    {typeof maxModelLen === 'number' && (
                        <div className="flex items-center justify-end gap-1.5 px-3 pt-2 text-[10px] text-slate-500">
                            <ContextUsagePie ratio={usedTokens / maxModelLen} />
                            <span title="Estimated context window usage for this chat">
                                {usedTokens.toLocaleString()} / {maxModelLen.toLocaleString()} tokens
                            </span>
                        </div>
                    )}
                    <div className="flex items-center gap-2 px-3 pt-2">
                        <div className="relative flex-1">
                            <input
                                className="w-full rounded-lg border border-slate-700 bg-slate-950 py-3.5 pl-3 pr-20 text-sm text-slate-100 disabled:opacity-50"
                                placeholder={
                                    transcribing
                                        ? 'Transcribing your voice message…'
                                        : recording
                                            ? 'Listening… click the mic again to stop'
                                            : activeService
                                                ? 'Message Lens…'
                                                : 'Select an AI provider on the right first'
                                }
                                value={input}
                                disabled={!activeService || sending || recording || transcribing || pendingConfirmCount > 0 || compacting}
                                onChange={(event) => setInput(event.target.value)}
                                onKeyDown={(event) => {
                                    if (event.key === 'Enter' && !event.shiftKey) {
                                        event.preventDefault();
                                        sendMessage();
                                    }
                                }}
                            />
                            <div className="absolute right-1.5 top-1/2 flex -translate-y-1/2 items-center gap-0.5">
                                {(() => {
                                    const micSupported = isSpeechToTextSupported();
                                    const micUnavailableReason = micSupported ? '' : getMicUnavailableReason();
                                    // Still render a (disabled) mic button when unsupported so the
                                    // reason is discoverable via the tooltip, instead of silently
                                    // disappearing and looking like a missing feature.
                                    return (
                                        <>
                                            {micSupported && (
                                                <select
                                                    value={micLanguage}
                                                    onChange={(event) => setMicLanguage(event.target.value)}
                                                    disabled={recording || transcribing}
                                                    title="Voice input language"
                                                    className="rounded border-none bg-transparent px-0.5 py-1 text-xs text-slate-400 focus:outline-none disabled:opacity-50"
                                                >
                                                    {SPEECH_TO_TEXT_LANGUAGES.map(({ value, label }) => (
                                                        <option key={value} value={value} className="bg-slate-900 text-slate-100">
                                                            {label}
                                                        </option>
                                                    ))}
                                                </select>
                                            )}
                                            <button
                                                type="button"
                                                onClick={micSupported ? (recording ? stopRecording : startRecording) : undefined}
                                                disabled={!micSupported || !activeService || sending || transcribing || compacting}
                                                title={
                                                    !micSupported
                                                        ? micUnavailableReason
                                                        : recording
                                                          ? 'Stop recording'
                                                          : 'Speak your question (runs locally in your browser)'
                                                }
                                                className={
                                                    'rounded p-1.5 transition-colors disabled:cursor-not-allowed disabled:opacity-40 ' +
                                                    (recording ? 'text-red-400 hover:text-red-300' : 'text-slate-400 hover:text-slate-100')
                                                }
                                            >
                                                {transcribing ? (
                                                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                                ) : recording ? (
                                                    <Square className="h-3.5 w-3.5" />
                                                ) : (
                                                    <Mic className="h-3.5 w-3.5" />
                                                )}
                                            </button>
                                        </>
                                    );
                                })()}
                            </div>
                        </div>
                    </div>
                    <p className="px-3 pb-2 pt-1 text-center text-[10px] text-slate-500">
                        llm-d Lens assistant is AI and can make mistakes.
                    </p>
                </div>

                <div className="flex w-full shrink-0 flex-col gap-4 lg:w-80">
                    <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
                        <div className="mb-3 flex items-center justify-between">
                            <label className="text-xs font-semibold uppercase tracking-wide text-slate-400">AI provider</label>
                            <Button
                                size="xs"
                                variant="ghost"
                                onClick={providerMode === 'internal' ? loadClusters : loadAiProviders}
                                disabled={providerMode === 'internal' ? loadingClusters : loadingAiProviders}
                            >
                                <RefreshCw className={'h-3 w-3 ' + ((providerMode === 'internal' ? loadingClusters : loadingAiProviders) ? 'animate-spin' : '')} />
                            </Button>
                        </div>

                        <div className="mb-3 flex gap-1 rounded-lg border border-slate-700 bg-slate-950 p-0.5 text-xs font-semibold">
                            <button
                                type="button"
                                onClick={() => setProviderMode('internal')}
                                className={
                                    'flex-1 rounded-md px-2 py-1.5 transition-colors ' +
                                    (providerMode === 'internal' ? 'bg-sky-500/20 text-sky-200' : 'text-slate-400 hover:text-slate-200')
                                }
                            >
                                Internal
                            </button>
                            <button
                                type="button"
                                onClick={() => setProviderMode('external')}
                                className={
                                    'flex-1 rounded-md px-2 py-1.5 transition-colors ' +
                                    (providerMode === 'external' ? 'bg-sky-500/20 text-sky-200' : 'text-slate-400 hover:text-slate-200')
                                }
                            >
                                External
                            </button>
                        </div>

                        <p className="mb-3 rounded-lg border border-amber-500/20 bg-amber-500/5 px-2.5 py-2 text-[11px] leading-snug text-amber-200/90">
                            Pick a model with a <span className="font-semibold">max context window of at least 32K tokens</span> (64K+
                            recommended). Lens Assistant sends its full tool catalog on every turn, so a smaller context window
                            leaves little room for actual conversation and tool results before older history has to be trimmed.
                            So far this has only been tested against <span className="font-semibold">deepseek-v4-pro</span> —
                            other models may behave differently, especially around tool-calling reliability.
                        </p>

                        {providerMode === 'internal' ? (
                            <>
                                <p className="mb-2 text-xs font-semibold text-slate-400">1. Cluster</p>
                                {clustersError && <p className="mb-2 text-xs text-red-400">{clustersError}</p>}
                                <select
                                    className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
                                    value={selectedClusterId}
                                    onChange={(event) => handleSelectCluster(event.target.value)}
                                    disabled={loadingClusters}
                                >
                                    <option value="">Select a Cluster…</option>
                                    {clusters.map((cluster) => (
                                        <option key={cluster.id} value={cluster.id} disabled={!cluster.ready}>
                                            {cluster.name}
                                            {!cluster.ready ? ' (not ready)' : ''}
                                        </option>
                                    ))}
                                </select>
                                {resolvingSession && <p className="mt-2 text-xs text-slate-400">Opening cluster session…</p>}

                                <p className="mb-2 mt-4 text-xs font-semibold text-slate-400">2. Deployment (model service)</p>
                                {!selectedClusterId ? (
                                    <p className="text-xs text-slate-400">Select a Cluster above first.</p>
                                ) : (
                                    <>
                                        {deploymentsError && <p className="mb-2 text-xs text-red-400">{deploymentsError}</p>}
                                        <select
                                            className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
                                            value={selectedId}
                                            onChange={(event) => setSelectedId(event.target.value)}
                                            disabled={!clusterSessionId || loadingDeployments || readyDeployments.length === 0}
                                        >
                                            <option value="" disabled>
                                                {loadingDeployments ? 'Loading…' : 'Select a Deployment…'}
                                            </option>
                                            {readyDeployments.map((deployment) => (
                                                <option key={deployment.id} value={deployment.id}>
                                                    {deployment.namespace} · {deployment.model}
                                                </option>
                                            ))}
                                        </select>

                                        {readyDeployments.length === 0 && !loadingDeployments && (
                                            <p className="mt-2 text-xs text-slate-400">
                                                No ready Deployment found yet. Deploy a model from Model Market first —
                                                Lens Assistant does not fall back to an external LLM.
                                            </p>
                                        )}
                                    </>
                                )}
                            </>
                        ) : (
                            <>
                                <p className="mb-2 text-xs font-semibold text-slate-400">External AI provider</p>
                                {aiProvidersError && <p className="mb-2 text-xs text-red-400">{aiProvidersError}</p>}
                                <select
                                    className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
                                    value={selectedProviderId}
                                    onChange={(event) => setSelectedProviderId(event.target.value)}
                                    disabled={loadingAiProviders || aiProviders.length === 0}
                                >
                                    <option value="" disabled>
                                        {loadingAiProviders ? 'Loading…' : 'Select an AI provider…'}
                                    </option>
                                    {aiProviders.map((provider) => (
                                        <option key={provider.id} value={provider.id}>
                                            {provider.name} · {provider.model}
                                        </option>
                                    ))}
                                </select>
                                {aiProviders.length === 0 && !loadingAiProviders && (
                                    <p className="mt-2 text-xs text-slate-400">
                                        No External AI providers configured yet. Add one under Resources → External providers
                                        (openai-type only — Anthropic providers aren't supported here yet).
                                    </p>
                                )}
                            </>
                        )}
                    </div>

                    {activeService && (
                        <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-4 text-xs text-emerald-200">
                            Talking to <span className="font-semibold">{activeService.label}</span>
                            {providerMode === 'internal' && selectedCluster ? (
                                <> on <span className="font-semibold">{selectedCluster.name}</span></>
                            ) : (
                                <> <span className="text-emerald-400/70">(External AI provider)</span></>
                            )}
                            .
                        </div>
                    )}
                </div>
            </div>
        </ModulePage>
    );
}

// Mirrors the backend's splitInlineThink (server/playground/chat.ts): some
// reasoning models emit their chain-of-thought inline as a
// <think>...</think> block rather than via a separate reasoning_content
// field. Kept here purely as a client-side safety net.
function splitInlineThink(content) {
    if (!content) return { think: null, answer: content ?? null };
    const match = content.match(/<think>([\s\S]*?)<\/think>/i);
    if (!match) return { think: null, answer: content };
    const think = match[1].trim();
    const answer = (content.slice(0, match.index) + content.slice(match.index + match[0].length)).trim();
    return { think: think || null, answer };
}

// Rebuilds the single "reasoning" message (think content + tool-call
// timeline, in that order, tightly grouped) that sits between the user's
// question and the pending assistant answer.
function withInterstitial(prev, thinkChunks, toolEntries) {
    const withoutInterstitial = prev.filter((message) => !(message.role === 'reasoning' && message.pending === true));
    const pendingIndex = withoutInterstitial.findIndex((message) => message.role === 'assistant' && message.pending);
    if (!thinkChunks.length && !toolEntries.length) return withoutInterstitial;
    const reasoning = {
        role: 'reasoning',
        think: thinkChunks.length ? thinkChunks.join('\n\n---\n\n') : null,
        entries: [...toolEntries],
        pending: true,
    };
    if (pendingIndex === -1) return [...withoutInterstitial, reasoning];
    return [...withoutInterstitial.slice(0, pendingIndex), reasoning, ...withoutInterstitial.slice(pendingIndex)];
}

// Mirrors the backend's post-answer history compaction (server/playground/
// chat.ts) against the client's own `messages` state, so the visible chat
// window matches whatever the next request will actually resend: drops the
// oldest `droppedCount` user/assistant bubbles from the front, along with
// any 'reasoning' entry that immediately precedes one of them (it annotates
// that same assistant turn and has no meaning on its own once it's gone).
function applyHistoryCompaction(prev, droppedCount) {
    if (!droppedCount) return prev;
    let remaining = droppedCount;
    let cutIndex = 0;
    while (cutIndex < prev.length && remaining > 0) {
        const message = prev[cutIndex];
        if (message.role === 'user' || message.role === 'assistant') {
            remaining -= 1;
        }
        cutIndex += 1;
    }
    return prev.slice(cutIndex);
}

// Ends a turn that produced a terminal outcome without a normal final answer
// (authorization stop, stream end): stop the "thinking" spinner and drop the
// empty pending assistant bubble so nothing keeps animating.
function finalizePendingTurn(prev) {
    return prev
        .map((message) => (message.role === 'reasoning' ? { ...message, pending: false } : message))
        .filter((message) => !(message.role === 'assistant' && message.pending));
}

function replacePendingAssistant(prev, content) {
    const next = prev.map((message) => (message.role === 'reasoning' ? { ...message, pending: false } : message));
    const pendingIndex = next.findIndex((message) => message.role === 'assistant' && message.pending);
    if (pendingIndex === -1) {
        if (content == null) return next;
        return [...next, { role: 'assistant', content }];
    }
    const updated = [...next];
    updated[pendingIndex] =
        content == null
            ? { role: 'assistant', content: 'Something went wrong before a final answer was produced.' }
            : { role: 'assistant', content };
    return updated;
}
