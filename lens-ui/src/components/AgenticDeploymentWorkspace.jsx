import { requestJson } from '../api/httpClient';
import { useEffect, useState } from 'react';
import { ArrowLeft, CheckCircle2, ChevronDown, Cpu, FileStack, LoaderCircle, RotateCcw, ShieldCheck, Sparkles, TriangleAlert } from 'lucide-react';
import { Badge, StatusChip } from './ui/Badge';
import { Button } from './ui/Button';
import { EmptyState } from './ui/EmptyState';
import { ModuleHeader } from './ui/ModuleHeader';
import { ModulePage } from './ui/ModulePage';
import { Select } from './ui/FormControls';
import { listAIProviders } from './AIProviders/aiProvidersBackend';
import { streamAgenticRecommendation, streamAgenticRefinement } from './agenticDeploymentBackend';
import { generatorLabel, historicalBenchmarkLabel, historicalEvidenceSummary, relaxedAicCandidates } from './agenticPresentation';

function api(path, options = {}) {
    return requestJson(path, { headers: { 'Content-Type': 'application/json' }, ...options });
}

const DEFAULT_REQUIREMENTS = {
    ttftSloMs: '', tpotSloMs: '', endToEndLatencySloMs: '',
    hardwareCapacityMode: 'free', customGpuCount: '',
    useCase: 'general-chat', inputTokens: '', outputTokens: '', concurrency: '', requestRate: '',
};

const GENERATION_STAGES = [
    { id: 'generation', label: 'Generating candidate', phases: ['initialization', 'validation', 'generation', 'ai', 'tools'], completionPhase: 'generation' },
    { id: 'candidate_validation', label: 'Validate candidate list', phases: ['candidate_validation'], completionPhase: 'candidate_validation' },
    { id: 'scoring', label: 'Scoring candidate', phases: ['scoring'], completionPhase: 'scoring' },
];

function GenerationProgress({ events }) {
    const [expandedStageId, setExpandedStageId] = useState(null);
    const stageEvents = GENERATION_STAGES.map((stage) => ({
        ...stage,
        events: events.filter((event) => stage.phases.includes(event.phase)),
    }));
    const latestStartedIndex = stageEvents.reduce(
        (latest, stage, index) => stage.events.length ? index : latest, 0,
    );

    return (
        <div role="status" aria-live="polite" className="mt-4 overflow-hidden border-t border-slate-800 pt-4">
            <div className="flex items-center gap-2 border-b border-slate-800 px-4 py-3">
                <LoaderCircle size={15} className="animate-spin text-cyan-300" />
                <h3 className="text-xs font-semibold text-slate-200">Generating recommendation</h3>
            </div>
            <div className="grid grid-cols-3 gap-2 bg-slate-950/45 px-4 py-4" role="progressbar" aria-label="Recommendation generation progress">
                {stageEvents.map((stage, index) => {
                    const started = stage.events.length > 0;
                    const finished = index < latestStartedIndex || stage.events.some((event) => (
                        event.phase === stage.completionPhase && (event.status === 'complete' || event.status === 'success' || event.status === 'fallback')
                    ));
                    const fallback = stage.events.some((event) => event.status === 'fallback');
                    const expanded = expandedStageId === stage.id;
                    return <button key={stage.id} type="button" disabled={!started} aria-expanded={expanded} onClick={() => setExpandedStageId(expanded ? null : stage.id)} className="min-w-0 text-left disabled:cursor-default">
                        <span className={`block h-1.5 overflow-hidden rounded-full ${fallback ? 'bg-amber-400' : finished ? 'bg-emerald-400/80' : 'bg-slate-800'}`}>
                            {started && !finished && !fallback && <span className="benchmark-progress block h-full w-1/3 rounded-full bg-cyan-400" />}
                        </span>
                        <span className={`mt-1 block text-[9px] leading-4 ${fallback ? 'text-amber-300' : expanded || started && !finished ? 'text-cyan-300' : finished ? 'text-emerald-300' : 'text-slate-500'}`}>{stage.label}</span>
                    </button>;
                })}
            </div>
            {expandedStageId && (() => {
                const stage = stageEvents.find((item) => item.id === expandedStageId);
                return <div className="max-h-56 space-y-2 overflow-y-auto border-t border-slate-800 bg-slate-950/70 px-4 py-3">
                    {stage.events.map((event, index) => (
                        <div key={`${event.phase}-${event.round || 0}-${index}`} className="border-l border-slate-700 pl-3">
                            <p className={`text-[11px] leading-4 ${event.status === 'fallback' ? 'text-amber-200' : 'text-slate-300'}`}>{event.message}</p>
                            {(event.round || event.duration_ms !== undefined) && <p className="mt-0.5 text-[10px] text-slate-500">{event.round ? `AI round ${event.round}` : ''}{event.round && event.duration_ms !== undefined ? ' · ' : ''}{event.duration_ms !== undefined ? `${event.duration_ms} ms` : ''}</p>}
                        </div>
                    ))}
                </div>;
            })()}
        </div>
    );
}

function CandidateRow({ candidate, selected, analysis, onDeploy, deploying, canDeploy }) {
    const prefill = candidate.prefill_replicas
        ? `Prefill ${candidate.prefill_replicas} x TP ${candidate.prefill_tensor_parallel_size}`
        : null;
    const historicalEvidence = historicalEvidenceSummary(candidate.historical_benchmarks);

    return (
        <div className={`grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-b border-slate-800/70 px-4 py-3 last:border-b-0 ${selected ? 'bg-emerald-500/8' : ''}`}>
            <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-xs font-semibold text-slate-100">{candidate.provider_ref}</span>
                    {selected && <Badge tone="success" size="xs">Recommended</Badge>}
                    {candidate.score !== null && candidate.score !== undefined && <Badge tone="info" size="xs">{candidate.score_source === 'openai-compatible' ? 'AI score' : 'Deterministic score'}: {Math.round(candidate.score * 100)}/100</Badge>}
                    <Badge tone={candidate.slo_status === 'satisfied' ? 'success' : candidate.slo_status === 'not_satisfied' ? 'danger' : 'neutral'} size="xs">
                        SLO {candidate.slo_status === 'not_satisfied' ? 'not met' : candidate.slo_status} ({Math.round((candidate.slo_confidence || 0) * 100)}%)
                    </Badge>
                </div>
                <p className="mt-1 truncate font-mono text-[10px] text-slate-500">{candidate.id}</p>
            </div>
            <div className="text-right text-[11px] text-slate-400">
                <div>{candidate.replicas} decode x TP {candidate.tensor_parallel_size}</div>
                {prefill && <div className="mt-1 text-slate-500">{prefill}</div>}
                <div className="mt-1 font-mono text-slate-300">{candidate.required_gpus} GPU</div>
                {candidate.allocatable_kv_cache_gib !== undefined && candidate.allocatable_kv_cache_gib !== null && (
                    <div className="mt-1 text-[10px] text-emerald-400 font-mono">
                        KV: {candidate.allocatable_kv_cache_gib} GiB{candidate.max_concurrent_requests ? ` · Max ${candidate.max_concurrent_requests} req` : ''}
                    </div>
                )}
                {canDeploy && <Button variant={selected ? 'primary' : 'outline'} size="xs" className="mt-2" onClick={() => onDeploy(candidate.id)} isLoading={deploying}><CheckCircle2 size={13} />Deploy recommendation</Button>}
            </div>
            <details className="col-span-full border-t border-slate-800/70 pt-2">
                <summary className="cursor-pointer text-[11px] text-slate-400">Why this option</summary>
                <div className="mt-2 space-y-1 text-[11px] leading-4 text-slate-400">
                    <p>{analysis.performance}</p>
                    <p>{analysis.resources}</p>
                    {candidate.allocatable_kv_cache_gib !== undefined && candidate.allocatable_kv_cache_gib !== null && (
                        <p className="text-cyan-300/90 font-mono text-[10px]">
                            Capacity: {candidate.allocatable_kv_cache_gib} GiB KV Cache allocatable{candidate.per_request_kv_cache_gib ? ` (${candidate.per_request_kv_cache_gib} GiB/req)` : ''}{candidate.max_concurrent_requests ? `, max ~${candidate.max_concurrent_requests} concurrent requests` : ''}.
                        </p>
                    )}
                    <p className="text-slate-500">{analysis.ranking}</p>
                    {historicalEvidence && <div className="border-t border-slate-800/70 pt-2">
                        <p className="font-medium text-slate-300">Historical benchmarks: {historicalEvidence.sampleCount} record(s), {historicalEvidence.successfulCount} succeeded, {historicalEvidence.failedCount} failed.</p>
                        <ul className="mt-1 space-y-1 font-mono text-[10px] text-slate-500">
                            {candidate.historical_benchmarks.map((sample) => <li key={sample.benchmark_id}>{historicalBenchmarkLabel(sample)}</li>)}
                        </ul>
                    </div>}
                </div>
            </details>
        </div>
    );
}

function evidenceLabel(item, hasAicPrediction) {
    if (item.id === 'aic:support' && hasAicPrediction) return null;
    if (item.id === 'aic:search') return 'AIC performance prediction';
    if (item.id.startsWith('cluster-overview:')) return 'Live cluster capacity';
    if (item.id === 'deploy-history') return 'Deployment history';
    if (item.id === 'simulation-history') return 'Simulation history';
    return item.source;
}

function fallbackSummary(reason) {
    if (reason === 'rate_limit') return 'The external planner is rate limited.';
    if (reason === 'timeout') return 'The external planner did not respond in time.';
    if (reason === 'transport') return 'The external planner could not be reached.';
    if (reason === 'invalid_response') return 'The external planner returned an invalid response.';
    return 'The external planner is unavailable, so Prism used its built-in ranking.';
}

function generatorFallbackSummary(reason) {
    if (reason === 'all_ai_candidates_invalid') return 'Every AI-generated candidate failed deterministic validation.';
    if (reason === 'mcp_unavailable') return 'The MCP planning tools could not be reached.';
    if (reason === 'invalid_response') return 'The AI generator returned an invalid candidate response.';
    return 'The configured AI generator could not be reached.';
}

export function candidateAnalysis(candidate, index, aicConfigs, relaxedConfigs, clusterDetails) {
    const aicApplicable = ['baseline-vllm', 'optimized-baseline', 'pd-disaggregation'].includes(candidate.provider_ref);
    const matchingPrediction = (config) => {
        const mode = config.topologyMode ?? config.mode;
        const decodeTp = config.decodeTp ?? config.decode_tp ?? config.tp;
        const decodeReplicas = config.decodeReplicas ?? config.decode_replicas ?? config.replicas;
        const prefillTp = config.prefillTp ?? config.prefill_tp;
        const prefillReplicas = config.prefillReplicas ?? config.prefill_replicas;
        return (mode === 'agg' && ['baseline-vllm', 'optimized-baseline'].includes(candidate.provider_ref)
            && Number(decodeTp) === candidate.tensor_parallel_size && Number(decodeReplicas) === candidate.replicas)
            || (mode === 'disagg' && candidate.provider_ref === 'pd-disaggregation'
                && Number(decodeTp) === candidate.tensor_parallel_size && Number(decodeReplicas) === candidate.replicas
                && Number(prefillTp) === candidate.prefill_tensor_parallel_size && Number(prefillReplicas) === candidate.prefill_replicas);
    };
    const prediction = aicConfigs.find(matchingPrediction);
    const relaxedPrediction = prediction ? null : relaxedConfigs.find(matchingPrediction);
    const metrics = prediction?.predicted || prediction;
    const relaxedMetrics = relaxedPrediction?.predicted;
    const estimate = candidate.performance_estimate;
    const availability = clusterDetails?.available_gpu_count;
    const performance = candidate.performance_estimate_source === 'aic_estimate' && estimate
        ? `AIC estimate (exact topology${candidate.slo_status === 'not_satisfied' ? '; original SLO not met' : ''}): TTFT ${estimate.ttft_ms?.toFixed(1) ?? 'n/a'} ms, TPOT ${estimate.tpot_ms?.toFixed(1) ?? 'n/a'} ms, throughput ${estimate.throughput_tokens_per_sec?.toFixed(1) ?? 'n/a'} tokens/s.`
        : prediction
        ? `AIC estimate: TTFT ${metrics.ttftMs?.toFixed(1) || metrics.ttft_ms?.toFixed(1) || 'n/a'} ms, TPOT ${metrics.tpotMs?.toFixed(1) || metrics.tpot_ms?.toFixed(1) || 'n/a'} ms, throughput ${metrics.throughputTps?.toFixed(1) || metrics.throughput_tokens_per_sec?.toFixed(1) || 'n/a'} tokens/s.`
        : relaxedPrediction
            ? `Relaxed AIC estimate (does not meet the original SLO): TTFT ${relaxedMetrics?.ttftMs?.toFixed(1) ?? 'n/a'} ms, TPOT ${relaxedMetrics?.tpotMs?.toFixed(1) ?? 'n/a'} ms, throughput ${relaxedMetrics?.throughputTps?.toFixed(1) ?? 'n/a'} tokens/s.`
        : aicApplicable
            ? 'No exact AIC topology prediction is available for this baseline or PD shape.'
            : 'AIC performance predictions do not apply to this guide; it is ranked using operator preference, guide suitability, and resource cost.';
    const resources = Number.isFinite(availability)
        ? `Hardware: requires ${candidate.required_gpus} GPU; ${availability} GPU are currently free. Capacity is rechecked immediately before deployment.`
        : `Hardware: requires ${candidate.required_gpus} GPU. Capacity is rechecked immediately before deployment.`;
    const ranking = candidate.performance_estimate_source === 'aic_estimate' && estimate
        ? `Rank ${index + 1}. This fixed-topology estimate informs the SLO assessment; ranking also considers operator preference, guide suitability, and GPU cost.`
        : prediction
        ? `Rank ${index + 1}. Exact AIC predictions are ordered by TTFT, then TPOT, then throughput before guide heuristics.`
        : aicApplicable
            ? `Rank ${index + 1}. No AIC prediction meets the original SLO, so this candidate is ordered by operator preference, guide suitability, and GPU cost.`
            : `Rank ${index + 1}. This guide has no comparable AIC prediction, so it is ordered by operator preference, guide suitability, and GPU cost.`;
    return { performance, resources, ranking };
}

export default function AgenticDeploymentWorkspace({ draft, embedded = false, onBack, onDeploymentStarted, vllmParameterControls }) {
    const [run, setRun] = useState(null);
    const [creating, setCreating] = useState(false);
    const [deployingCandidateId, setDeployingCandidateId] = useState('');
    const [error, setError] = useState('');
    const [requirements, setRequirements] = useState(DEFAULT_REQUIREMENTS);
    const [plannerPrompt, setPlannerPrompt] = useState('');
    const [aiProviders, setAiProviders] = useState([]);
    const [aiProviderId, setAiProviderId] = useState('');
    const [refinementPrompt, setRefinementPrompt] = useState('');
    const [refining, setRefining] = useState(false);
    const [generationEvents, setGenerationEvents] = useState([]);
    const [refinementEvents, setRefinementEvents] = useState([]);
    const updateRequirement = (name) => (event) => setRequirements((current) => ({ ...current, [name]: event.target.value }));
    const numeric = (value) => (value ? Number(value) : null);

    useEffect(() => {
        let cancelled = false;
        listAIProviders().then((items) => {
            if (cancelled) return;
            setAiProviders(items);
            // Auto-select the first saved provider so the operator doesn't have
            // to manually pick one to get AI-assisted ranking by default.
            setAiProviderId((current) => current || (items[0]?.id ?? ''));
        }).catch(() => {});
        return () => { cancelled = true; };
    }, []);

    const createPlan = async () => {
        if (!draft?.storageVolumeId) {
            setError('Select a model cache storage before creating an Agentic recommendation.');
            return;
        }
        setCreating(true);
        setError('');
        setGenerationEvents([{ phase: 'initialization', status: 'running', message: 'Connecting to the recommendation service.' }]);
        try {
            const session = await api(`/api/cluster/session?clusterId=${encodeURIComponent(draft.clusterId)}`);
            const clusterSessionId = session.sessionId || session.session_id;
            if (!clusterSessionId) throw new Error('The selected cluster did not return an active session');
            const payload = {
                model: draft.model, deployment_name: draft.deploymentName, description: draft.description || '',
                cluster_session_id: clusterSessionId, replicas: 1, tensor_parallel_size: 1,
                storage_type: 'model-cache', storage_volume_id: draft.storageVolumeId,
                max_model_len: draft.contextLength, gpu_memory_utilization: draft.gpuMemoryUtilization,
                max_num_seqs: draft.maxNumSeqs, max_num_batched_tokens: draft.maxNumBatchedTokens,
                vllm_arguments: draft.vllmArguments || [],
                planning_facts: {
                    model_weight_gib: draft.modelWeightGiB, vram_per_gpu_gib: 1, free_gpu_count: 0,
                    hardware_capacity_mode: requirements.hardwareCapacityMode,
                    custom_gpu_count: requirements.hardwareCapacityMode === 'custom' ? numeric(requirements.customGpuCount) : null,
                    context_length: draft.contextLength, use_case: requirements.useCase,
                    ttft_slo_ms: numeric(requirements.ttftSloMs), tpot_slo_ms: numeric(requirements.tpotSloMs),
                    end_to_end_latency_slo_ms: numeric(requirements.endToEndLatencySloMs),
                    workload_profile: {
                        mean_input_tokens: numeric(requirements.inputTokens),
                        p95_input_tokens: numeric(requirements.inputTokens),
                        mean_output_tokens: numeric(requirements.outputTokens),
                        concurrency: numeric(requirements.concurrency),
                        request_rate: numeric(requirements.requestRate),
                    },
                },
                planner_prompt: plannerPrompt.trim() || null,
                ai_provider_id: aiProviderId || null,
            };
            let created = null;
            await streamAgenticRecommendation(payload, (type, data) => {
                if (type === 'progress') setGenerationEvents((current) => [...current.slice(-59), data]);
                if (type === 'complete') created = data;
                if (type === 'error') throw new Error(data.message || 'Unable to generate an Agentic recommendation');
            });
            if (!created) throw new Error('Recommendation stream ended before returning a result');
            setRun(created);
        } catch (failure) {
            setError(failure.message || 'Unable to generate an Agentic recommendation');
        } finally {
            setCreating(false);
        }
    };

    const refinePlan = async () => {
        if (!run || !refinementPrompt.trim()) return;
        setRefining(true);
        setError('');
        setRefinementEvents([{ phase: 'initialization', status: 'running', message: 'Connecting to the recommendation service.' }]);
        try {
            let refined = null;
            await streamAgenticRefinement(run.id, { planner_prompt: refinementPrompt.trim() }, (type, data) => {
                if (type === 'progress') setRefinementEvents((current) => [...current.slice(-59), data]);
                if (type === 'complete') refined = data;
                if (type === 'error') throw new Error(data.message || 'Unable to recalculate the recommendation');
            });
            if (!refined) throw new Error('Recalculation stream ended before returning a result');
            setRun(refined);
            setRefinementPrompt('');
        } catch (failure) {
            setError(failure.message || 'Unable to recalculate the recommendation');
        } finally {
            setRefining(false);
        }
    };

    const reset = () => {
        setRun(null);
        setRequirements(DEFAULT_REQUIREMENTS);
        setPlannerPrompt('');
        setRefinementPrompt('');
        setGenerationEvents([]);
        setRefinementEvents([]);
        setError('');
    };

    const deployCandidate = async (candidateId) => {
        if (!run) return;
        setDeployingCandidateId(candidateId);
        setError('');
        try {
            if (run.selected_candidate?.id !== candidateId) {
                const updated = await api(`/api/agentic-deployments/${encodeURIComponent(run.id)}/select`, {
                    method: 'POST', body: JSON.stringify({ candidate_id: candidateId }),
                });
                setRun(updated);
            }
            await api(`/api/agentic-deployments/${encodeURIComponent(run.id)}/approve`, { method: 'POST' });
            onDeploymentStarted();
        } catch (failure) {
            setError(failure.message || 'Unable to start deployment');
        } finally {
            setDeployingCandidateId('');
        }
    };

    const input = (label, name, props = {}) => (
        <label className="text-xs text-slate-400">{label}
            <input className="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 text-sm text-slate-200" type="number" placeholder="Optional" value={requirements[name]} onChange={updateRequirement(name)} {...props} />
        </label>
    );
    const hasAicPrediction = run?.planning_evidence?.some((item) => item.id === 'aic:search' && item.status === 'available');
    const canDeploy = run?.status === 'awaiting_approval';
    const aicConfigs = run?.planning_evidence?.find((item) => item.id === 'aic:search')?.details?.candidates?.candidates || [];
    const relaxedConfigs = relaxedAicCandidates(run);
    const clusterDetails = run?.planning_evidence?.find((item) => item.id.startsWith('cluster-overview:'))?.details;
    const usedAiProviderName = aiProviders.find((provider) => provider.id === run?.request?.ai_provider_id)?.name;

    const content = (
        <>
            {!embedded && <ModuleHeader icon={Sparkles} title="Agentic deployment recommendation" description="Prism chooses a supported guide from the model, live cluster capacity, AIC, and prior evidence." actions={<div className="flex gap-2"><Button variant="secondary" onClick={reset}><RotateCcw size={15} />Reset</Button><Button variant="secondary" onClick={onBack}><ArrowLeft size={15} />Model details</Button></div>} />}
            {embedded && <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold text-white">Agentic deployment plan</h2><p className="mt-1 text-sm text-slate-400">Prism chooses a supported guide from live cluster capacity and available evidence.</p></div><Button variant="secondary" onClick={reset}><RotateCcw size={15} />Reset</Button></div>}
            <div className="rounded-lg border border-slate-800 bg-slate-900/45 p-3">
                <label className="block text-xs font-semibold text-slate-300">AI provider <span className="font-normal text-slate-500">(optional; powers the external planner used to rank candidates)</span></label>
                <Select className="mt-1.5" value={aiProviderId} onChange={(event) => setAiProviderId(event.target.value)}>
                    <option value="">Default (no external AI provider)</option>
                    {aiProviders.map((provider) => (
                        <option key={provider.id} value={provider.id}>{provider.name}</option>
                    ))}
                </Select>
                {aiProviders.length === 0 && (
                    <p className="mt-1.5 text-xs text-slate-500">No AI providers configured yet. Add one under Resources → External providers.</p>
                )}
            </div>
            {error && <div role="alert" className="flex items-center gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200"><TriangleAlert size={15} />{error}</div>}
            {!run && <section className="flex flex-col gap-4">
                <div className="rounded-lg border border-slate-800 bg-slate-900/45 p-5">
                    <div className="grid gap-3">
                        <label className="text-xs text-slate-400">Use case
                            <Select className="mt-1" value={requirements.useCase} onChange={updateRequirement('useCase')}>
                                <option value="general-chat">General chat</option>
                                <option value="code-generation">Code generation</option>
                                <option value="long-inputs">Long-input analysis</option>
                                <option value="summarization">Document summarization</option>
                            </Select>
                        </label>
                        {input('TTFT SLO (ms)', 'ttftSloMs', { min: '1' })}
                        {input('TPOT SLO (ms)', 'tpotSloMs', { min: '1' })}
                        {input('End-to-end latency (ms)', 'endToEndLatencySloMs', { min: '1' })}
                        <label className="text-xs text-slate-400">GPU capacity for planning
                            <Select className="mt-1" value={requirements.hardwareCapacityMode} onChange={updateRequirement('hardwareCapacityMode')}>
                                <option value="free">Currently free GPUs</option>
                                <option value="all">All cluster GPUs</option>
                                <option value="custom">Custom GPU count</option>
                            </Select>
                        </label>
                        {requirements.hardwareCapacityMode === 'custom' && input('Custom GPU count', 'customGpuCount', { min: '1', step: '1', required: true })}
                    </div>
                    <details open className="mt-4 border-t border-slate-800 pt-3">
                        <summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-semibold text-slate-300"><ChevronDown size={14} />Preference <span className="font-normal text-slate-500">Optional deployment preference.</span></summary>
                        <textarea className="mt-3 min-h-20 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm text-slate-200" value={plannerPrompt} onChange={(event) => setPlannerPrompt(event.target.value)} maxLength={2000} placeholder="Describe your deployment preference." />
                    </details>
                    {vllmParameterControls && <details className="mt-4 border-t border-slate-800 pt-3">
                        <summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-semibold text-slate-300"><ChevronDown size={14} />vLLM parameters</summary>
                        <div className="mt-3 grid gap-3">{vllmParameterControls}</div>
                    </details>}
                    <details className="mt-4 border-t border-slate-800 pt-3">
                        <summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-semibold text-slate-300"><ChevronDown size={14} />Workload estimate <span className="font-normal text-slate-500">Optional request shape and demand.</span></summary>
                        <div className="mt-3 grid gap-3 sm:grid-cols-2">
                            {input('Input tokens per request', 'inputTokens', { min: '1', step: '1' })}
                            {input('Output tokens per request', 'outputTokens', { min: '1', step: '1' })}
                            {input('Concurrent requests', 'concurrency', { min: '1', step: '1' })}
                            {input('Request rate (QPS)', 'requestRate', { min: '0.01', step: 'any' })}
                        </div>
                    </details>
                    {creating && <GenerationProgress events={generationEvents} />}
                    <div className="mt-5 flex justify-end"><Button onClick={createPlan} isLoading={creating}><Sparkles size={16} />Generate recommendation</Button></div>
                </div>
            </section>}
            {run && <section className="flex flex-col gap-5">
                <div className="rounded-lg border border-slate-800 bg-slate-900/65 p-5"><div className="flex flex-wrap items-center gap-2"><h2 className="text-lg font-semibold text-white">{run.selected_candidate?.provider_ref}</h2><StatusChip status={run.status} label={run.status.replaceAll('_', ' ')} /><Badge tone={run.generator === 'ai-mcp' ? 'info' : 'neutral'} size="xs">{generatorLabel(run.generator)}</Badge></div><p className="mt-2 text-xs text-slate-400">Recommended for {draft.model}</p><p className="mt-2 text-xs leading-5 text-slate-300">{run.decision?.rationale}</p></div>
                {run.generator_fallback_reason && <div className="rounded-lg border border-amber-500/25 bg-amber-500/10 p-3 text-xs text-amber-100"><span className="font-semibold">Built-in candidate generation used.</span> {generatorFallbackSummary(run.generator_fallback_reason)}</div>}
                {run.planner_fallback_reason && <div className="rounded-lg border border-amber-500/25 bg-amber-500/10 p-3 text-xs text-amber-100"><span className="font-semibold">Built-in ranking used.</span> {fallbackSummary(run.planner_fallback_reason)}</div>}
                <div className="grid gap-4">
                    <div className="overflow-hidden rounded-lg border border-slate-800 bg-slate-900/45"><div className="flex items-center gap-2 border-b border-slate-800 bg-slate-950/55 px-4 py-3"><Cpu size={15} className="text-cyan-300" /><h3 className="text-xs font-semibold text-slate-200">Validated alternatives</h3></div>{run.candidates?.map((candidate, index) => <CandidateRow key={candidate.id} candidate={candidate} selected={candidate.id === run.selected_candidate?.id} analysis={candidateAnalysis(candidate, index, aicConfigs, relaxedConfigs, clusterDetails)} onDeploy={deployCandidate} deploying={deployingCandidateId === candidate.id} canDeploy={canDeploy} />)}{canDeploy && <div className="border-t border-slate-800 p-3"><textarea className="min-h-20 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-xs text-slate-200" value={refinementPrompt} onChange={(event) => setRefinementPrompt(event.target.value)} maxLength={2000} placeholder="Describe unmet needs. The external planner can choose and score up to ten validated candidates." />{refining && <GenerationProgress events={refinementEvents} />}<div className="mt-2 flex justify-end"><Button size="xs" variant="secondary" onClick={refinePlan} isLoading={refining} disabled={!refinementPrompt.trim()}>Recalculate selection</Button></div></div>}</div>
                    <div className="rounded-lg border border-slate-800 bg-slate-900/45"><div className="flex items-center gap-2 border-b border-slate-800 bg-slate-950/55 px-4 py-3"><FileStack size={15} className="text-cyan-300" /><h3 className="text-xs font-semibold text-slate-200">Decision evidence</h3></div><div className="divide-y divide-slate-800/70">
                        {run.decision_metadata && <div className="p-3"><p className="text-xs font-semibold text-slate-200">{run.decision_metadata.planner === 'openai-compatible' ? 'External planner' : 'Prism deterministic planner'}</p><p className="mt-1 text-[11px] text-slate-400">Candidates: {run.generator === 'ai-mcp' ? 'AI-generated with MCP evidence' : 'Prism deterministic generator'}</p>{usedAiProviderName && <p className="mt-1 text-[11px] text-slate-400">AI provider: {usedAiProviderName}</p>}{run.generator_model && <p className="mt-1 text-[11px] text-slate-400">Generator model: {run.generator_model}</p>}{run.decision_metadata.planner_model && <p className="mt-1 text-[11px] text-slate-400">Scoring model: {run.decision_metadata.planner_model}</p>}<p className="mt-2 text-[11px] leading-4 text-slate-400">{run.decision_metadata.selection_method}</p><p className="mt-2 text-[11px] leading-4 text-slate-500">{run.decision_metadata.score_method}</p></div>}
                        {run.planning_evidence?.length ? run.planning_evidence.map((item) => { const label = evidenceLabel(item, hasAicPrediction); if (!label) return null; const used = (run.decision_evidence_ids || []).includes(item.id); return <div key={`${item.source}:${item.id}`} className="p-3"><div className="flex items-center justify-between gap-2"><span className="text-xs text-slate-200">{label}</span><Badge tone={used ? 'success' : 'neutral'} size="xs">{used ? 'used for selection' : 'context only'}</Badge></div>{item.id === 'aic:search' && <p className="mt-1 text-[10px] text-slate-500">Support was verified before the prediction search.</p>}</div>; }) : <EmptyState icon={<ShieldCheck size={24} />} title="No evidence available" message="The recommendation remains constrained by the live cluster checks." />}
                    </div></div>
                </div>
            </section>}
        </>
    );
    return embedded ? <section className="flex flex-col gap-5">{content}</section> : <ModulePage contentClassName="flex flex-col gap-5">{content}</ModulePage>;
}
