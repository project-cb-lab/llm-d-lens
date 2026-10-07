import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    Area,
    CartesianGrid,
    ComposedChart,
    Line,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import {
    Activity,
    AlertTriangle,
    ArrowLeft,
    ChevronLeft,
    ChevronRight,
    Clock3,
    Download,
    FileJson,
    Gauge,
    Info,
    RefreshCw,
    Square,
    Trash2,
} from 'lucide-react';
import { Button, EmptyState, Modal, Panel, Spinner, StatusChip } from './ui';
import { confirmDelete } from './ui/confirmDelete';
import { downloadFile } from '../utils/download';
import { formatTimestamp } from '../utils/formatTimestamp';
import { getDeploymentExecutions } from './OptimizationWorkspace/remoteDeployBackend';
import { RESPONSE_CODE_ISSUE_PAGE_SIZE, RUNNING_STATUSES, STOPPABLE_STATUSES, TERMINAL_STATUSES, arrayFrom, backendOptionLabel, backendOptionValue, displayName, findNamedSection, formatCount, formatDuration, formatMilliseconds, formatNumber, numberAt, responseStatusGuidance, scenarioDisplayName, taskBackend, taskDataset, taskStatusChip, unwrapTask } from '../features/simulation/presentation';
import { requestJson } from '../features/simulation/client';
import { ConfigurationGroup } from './Simulation/ConfigurationGroup';
import { DetailCard } from './Simulation/DetailCard';
import { JsonBlock } from './Simulation/JsonBlock';
import { LatencyHeatmap } from './Simulation/LatencyHeatmap';
import { LatencyMetricTimeline } from './Simulation/LatencyMetricTimeline';
import { MetricBars } from './Simulation/MetricBars';
import { StatisticsPanel } from './Simulation/StatisticsPanel';

export default function SimulationTaskDetails({ onNavigate }) {
    const params = new URLSearchParams(window.location.search);
    const routeTaskId = params.get('taskId') || '';

    const [backends, setBackends] = useState([]);
    const [task, setTask] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [actionError, setActionError] = useState('');
    const [detailTab, setDetailTab] = useState('summary');
    const [durationClockMs, setDurationClockMs] = useState(Date.now);
    const [stopping, setStopping] = useState(false);
    const [stoppingTaskId, setStoppingTaskId] = useState('');
    const [rerunningTaskId, setRerunningTaskId] = useState('');
    const [deletingTaskId, setDeletingTaskId] = useState('');
    const [downloadingArtifact, setDownloadingArtifact] = useState('');
    const [artifactDownloadError, setArtifactDownloadError] = useState('');
    const [selectedResponseCode, setSelectedResponseCode] = useState(undefined);
    const [responseCodeIssues, setResponseCodeIssues] = useState([]);
    const [responseCodeIssueTotal, setResponseCodeIssueTotal] = useState(0);
    const [responseCodeIssuePage, setResponseCodeIssuePage] = useState(0);
    const [responseCodeIssuesLoading, setResponseCodeIssuesLoading] = useState(false);
    const [responseCodeIssuesError, setResponseCodeIssuesError] = useState('');
    const taskLogRef = useRef(null);

    const taskId = task?.id || task?.task_id || routeTaskId;
    const taskStatus = String(task?.status || '').toLowerCase();
    const taskStartedAt = task?.started_at;
    const taskLogs = useMemo(() => (Array.isArray(task?.logs) ? task.logs : []), [task?.logs]);

    const closeResponseCodeIssues = useCallback(() => {
        setSelectedResponseCode(undefined);
        setResponseCodeIssues([]);
        setResponseCodeIssueTotal(0);
        setResponseCodeIssuePage(0);
        setResponseCodeIssuesError('');
    }, []);

    const loadBackends = useCallback(async () => {
        try {
            const payload = await requestJson('/api/v1/simulation/backends');
            setBackends(arrayFrom(payload, ['backends', 'items', 'data']));
        } catch {
            setBackends([]);
        }
    }, []);

    const loadTaskDetails = useCallback(async ({ quiet = false } = {}) => {
        if (!routeTaskId) {
            setError('A simulation task ID is required.');
            setLoading(false);
            return;
        }
        if (!quiet) {
            setLoading(true);
            setActionError('');
        }
        try {
            const payload = await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(routeTaskId)}`);
            setTask(unwrapTask(payload));
            setError('');
        } catch (nextError) {
            setError(`Unable to load task details: ${nextError.message}`);
            if (!quiet) setTask(null);
        } finally {
            if (!quiet) setLoading(false);
        }
    }, [routeTaskId]);

    useEffect(() => {
        setTask(null);
        setDetailTab('summary');
        setActionError('');
        setArtifactDownloadError('');
        closeResponseCodeIssues();
        void loadTaskDetails();
    }, [closeResponseCodeIssues, loadTaskDetails, routeTaskId]);

    useEffect(() => {
        void loadBackends();
    }, [loadBackends]);

    useEffect(() => {
        if (!taskStartedAt || !RUNNING_STATUSES.has(taskStatus)) return undefined;
        setDurationClockMs(Date.now());
        const interval = window.setInterval(() => setDurationClockMs(Date.now()), 1000);
        return () => window.clearInterval(interval);
    }, [taskStartedAt, taskStatus]);

    useEffect(() => {
        if (taskStatus !== 'running' || detailTab !== 'errors' || !taskLogs.length) return undefined;
        const frame = window.requestAnimationFrame(() => {
            if (taskLogRef.current) {
                taskLogRef.current.scrollTop = taskLogRef.current.scrollHeight;
            }
        });
        return () => window.cancelAnimationFrame(frame);
    }, [detailTab, taskLogs, taskStatus]);

    useEffect(() => {
        if (taskStatus === 'running' && ['latency', 'throughput'].includes(detailTab)) {
            setDetailTab('summary');
        }
    }, [detailTab, taskStatus]);

    useEffect(() => {
        if (!RUNNING_STATUSES.has(taskStatus)) return undefined;
        let active = true;
        let timeout;
        const poll = async () => {
            if (!active) return;
            await loadTaskDetails({ quiet: true });
            if (active) timeout = window.setTimeout(poll, 1000);
        };
        void poll();
        return () => {
            active = false;
            if (timeout) window.clearTimeout(timeout);
        };
    }, [loadTaskDetails, taskStatus]);

    const openResponseCodeIssues = useCallback(async (statusCode, page = 0) => {
        if (!taskId) return;
        setSelectedResponseCode(statusCode);
        setResponseCodeIssuePage(page);
        setResponseCodeIssues([]);
        if (page === 0) setResponseCodeIssueTotal(0);
        setResponseCodeIssuesError('');
        setResponseCodeIssuesLoading(true);
        try {
            const encodedCode = statusCode === null ? 'unknown' : String(statusCode);
            const offset = page * RESPONSE_CODE_ISSUE_PAGE_SIZE;
            const payload = await requestJson(
                `/api/v1/simulation/tasks/${encodeURIComponent(taskId)}/response-code-issues?status_code=${encodeURIComponent(encodedCode)}&offset=${offset}&limit=${RESPONSE_CODE_ISSUE_PAGE_SIZE}`
            );
            setResponseCodeIssues(Array.isArray(payload?.issues) ? payload.issues : []);
            setResponseCodeIssueTotal(Number(payload?.total ?? 0));
        } catch (nextError) {
            setResponseCodeIssuesError(`Unable to load request issues: ${nextError.message}`);
        } finally {
            setResponseCodeIssuesLoading(false);
        }
    }, [taskId]);

    const downloadArtifact = useCallback(async (artifact) => {
        if (!taskId || !artifact?.kind) return;
        setDownloadingArtifact(artifact.kind);
        setArtifactDownloadError('');
        try {
            await downloadFile(
                `/api/v1/simulation/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifact.kind)}`,
                { fallbackName: artifact.name || artifact.kind },
            );
        } catch (nextError) {
            setArtifactDownloadError(nextError.message);
        } finally {
            setDownloadingArtifact('');
        }
    }, [taskId]);

    const stopTask = useCallback(async () => {
        if (!taskId || !task) return;
        setStopping(true);
        setStoppingTaskId(taskId);
        setActionError('');
        try {
            const payload = await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(taskId)}/stop`, {
                method: 'POST',
            });
            setTask((current) => current ? {
                ...current,
                status: payload?.status || 'cancelling',
                progress_message: 'Cancellation requested',
            } : current);
        } catch (nextError) {
            setActionError(`Unable to stop simulation: ${nextError.message}`);
        } finally {
            setStopping(false);
            setStoppingTaskId('');
        }
    }, [task, taskId]);

    const resolveLegacyRerunBody = useCallback(async () => {
        if (!task || String(task?.endpoint_mode || '').toLowerCase() !== 'deployment') return null;
        if (task?.endpoint_deployment_execution_id || task?.endpoint_deployment_run_id || task?.endpoint_deployment_case_id) {
            return null;
        }
        const payload = await requestJson('/api/cluster/clusters');
        const clusters = (payload?.items || []).filter((cluster) => cluster.ready);
        if (!clusters.length) return null;
        const cluster = clusters.find((candidate) => candidate.id === task.endpoint_cluster_id) || clusters[0];
        const deployments = (await getDeploymentExecutions({ status: 'ready', clusterId: cluster.id })).filter(
            (deployment) => deployment.execution_id && deployment.endpoint,
        );
        if (!deployments.length) return null;
        const deployment = deployments.find((candidate) => (
            candidate.execution_id === task.endpoint_deployment_execution_id
            || candidate.name === task.endpoint_deployment_name
            || candidate.endpoint === task.endpoint_url
            || candidate.model === task.model_name
        )) || deployments[0];
        return {
            endpoint_deployment_execution_id: deployment.execution_id,
            endpoint_cluster_id: cluster.id,
            endpoint_cluster_name: cluster?.name || null,
            endpoint_deployment_name: deployment?.name || null,
        };
    }, [task]);

    const rerunSimulationTask = useCallback(async () => {
        if (!task || !taskId || RUNNING_STATUSES.has(taskStatus)) return;
        setRerunningTaskId(taskId);
        setActionError('');
        try {
            const rerunBody = await resolveLegacyRerunBody();
            const payload = await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(taskId)}/rerun`, {
                method: 'POST',
                headers: rerunBody ? { 'Content-Type': 'application/json' } : undefined,
                body: rerunBody ? JSON.stringify(rerunBody) : undefined,
            });
            const rerun = unwrapTask(payload);
            const rerunId = rerun?.id || payload?.task_id;
            if (!rerunId) throw new Error('The rerun response did not include a task ID.');
            onNavigate('optimization-simulate-details', { taskId: rerunId });
        } catch (nextError) {
            setActionError(`Unable to rerun simulation: ${nextError.message}`);
        } finally {
            setRerunningTaskId('');
        }
    }, [onNavigate, resolveLegacyRerunBody, task, taskId, taskStatus]);

    const deleteSimulationTask = useCallback(async () => {
        if (!task || !taskId) return;
        if (RUNNING_STATUSES.has(taskStatus)) {
            setActionError('Stop this task before deleting it.');
            return;
        }
        const confirmed = await confirmDelete(`Delete simulation task ${task.name || taskId}? This cannot be undone.`);
        if (!confirmed) return;
        setDeletingTaskId(taskId);
        setActionError('');
        try {
            await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
            onNavigate('optimization-simulate');
        } catch (nextError) {
            setActionError(`Unable to delete simulation task: ${nextError.message}`);
        } finally {
            setDeletingTaskId('');
        }
    }, [onNavigate, task, taskId, taskStatus]);

    const resultPayload = task?.result || task?.results || task?.output || null;
    const result = Array.isArray(resultPayload) ? resultPayload[0] || null : resultPayload;
    const summary = result?.summary || task?.live_summary || task?.summary || {};
    const artifacts = result?.artifacts || task?.artifacts || [];
    const backendMetrics = result?.backend_metrics || result?.backendMetrics || {};
    const timing = result?.timing || summary?.timing || backendMetrics?.timing
        || findNamedSection(backendMetrics, /timing|schedule/i);
    const drift = result?.drift || summary?.drift || backendMetrics?.drift || backendMetrics?.schedule_drift
        || findNamedSection(backendMetrics, /drift/i);
    const progress = Math.max(0, Math.min(100, Number(task?.progress_percent ?? task?.progress ?? 0)));
    const totalRequests = numberAt(summary, ['total_requests', 'requests_total', 'request_count']);
    const successfulRequests = numberAt(summary, ['successful_requests', 'requests_success', 'success_count']);
    const failedRequests = numberAt(summary, ['failed_requests', 'requests_failed', 'error_count']);
    const requestRate = numberAt(summary, ['request_throughput.avg', 'throughput_rps', 'requests_per_second']);
    const tokenRate = numberAt(summary, ['output_token_throughput.avg', 'throughput_tps', 'tokens_per_second']);
    const inputTokenRate = numberAt(summary, ['input_throughput_tps', 'input_token_throughput.avg']);
    const totalTokenRate = numberAt(summary, ['total_throughput_tps', 'total_token_throughput.avg']);
    const effectiveConcurrency = numberAt(summary, ['effective_concurrency']);
    const totalInputTokens = numberAt(summary, ['total_input_tokens', 'input_tokens_total']);
    const totalOutputTokens = numberAt(summary, ['total_output_tokens', 'output_tokens_total']);
    const nonStreamingAiperf = taskBackend(task) === 'aiperf' && task?.simulation?.stream === false;
    const resultWarnings = Array.isArray(result?.warnings) ? [...result.warnings] : [];
    if (nonStreamingAiperf && !resultWarnings.some((warning) => warning.includes('non-streaming'))) {
        resultWarnings.push('TTFT and TPOT metrics are unavailable because this task used non-streaming responses. Enable Stream responses to collect them.');
    }
    const liveCompletionTotals = Array.isArray(summary?.completion_timeline)
        ? summary.completion_timeline.reduce((totals, point) => ({
            successful: totals.successful + Number(point.successful_requests || 0),
            failed: totals.failed + Number(point.failed_requests || 0),
        }), { successful: 0, failed: 0 })
        : null;
    const liveCompletedRequests = liveCompletionTotals
        ? liveCompletionTotals.successful + liveCompletionTotals.failed
        : 0;
    const displayedTotalRequests = totalRequests ?? (liveCompletedRequests > 0 ? liveCompletedRequests : null);
    const displayedFailedRequests = failedRequests
        ?? (liveCompletionTotals && liveCompletedRequests > 0 ? liveCompletionTotals.failed : null);
    const successRate = numberAt(summary, ['success_rate'])
        ?? (totalRequests !== null && totalRequests > 0 && successfulRequests !== null
            ? successfulRequests / totalRequests * 100
            : liveCompletedRequests > 0
                ? liveCompletionTotals.successful / liveCompletedRequests * 100
                : null);
    const errorRate = displayedTotalRequests > 0 && displayedFailedRequests !== null
        ? displayedFailedRequests / displayedTotalRequests * 100
        : successRate === null ? null : Math.max(0, 100 - successRate);
    const ttft = nonStreamingAiperf ? {} : summary?.ttft || summary?.time_to_first_token || {};
    const tpot = nonStreamingAiperf ? {} : summary?.tpot || summary?.inter_token_latency || {};
    const latencyDistribution = summary?.latency || summary?.request_latency || {};
    const driftDistribution = summary?.drift || summary?.schedule_drift
        || (drift && Object.values(drift)[0] && typeof Object.values(drift)[0] === 'object' ? Object.values(drift)[0] : drift)
        || {};
    const ttftMean = numberAt(ttft, ['mean_ms', 'avg_ms', 'mean', 'avg']);
    const tpotMean = numberAt(tpot, ['mean_ms', 'avg_ms', 'mean', 'avg']);
    const averageInputTokens = successfulRequests > 0 && totalInputTokens !== null ? totalInputTokens / successfulRequests : null;
    const averageOutputTokens = successfulRequests > 0 && totalOutputTokens !== null ? totalOutputTokens / successfulRequests : null;
    const totalTokens = totalInputTokens !== null || totalOutputTokens !== null
        ? (totalInputTokens || 0) + (totalOutputTokens || 0)
        : null;
    const backendVersion = result?.backend_version || task?.backend_version
        || backends.find((item) => (item.name || item.id || item.value) === taskBackend(task))?.version;
    const backendOptions = task?.simulation?.backend_options || task?.backend_options || {};
    const backendOptionEntries = Object.entries(backendOptions).filter(
        ([key]) => !['ttft_slo', 'tpot_slo', 'error_rate_slo'].includes(key)
    );
    const ttftSloSeconds = numberAt(backendOptions, ['ttft_slo']);
    const tpotSloSeconds = numberAt(backendOptions, ['tpot_slo']);
    const errorRateSlo = numberAt(backendOptions, ['error_rate_slo']);
    const ttftSloMs = ttftSloSeconds === null ? null : ttftSloSeconds * 1000;
    const tpotSloMs = tpotSloSeconds === null ? null : tpotSloSeconds * 1000;
    const ttftAccent = ttftMean === null || ttftSloMs === null
        ? 'text-slate-100'
        : ttftMean > ttftSloMs ? 'text-rose-300' : 'text-emerald-300';
    const tpotAccent = tpotMean === null || tpotSloMs === null
        ? 'text-slate-100'
        : tpotMean > tpotSloMs ? 'text-rose-300' : 'text-emerald-300';
    const errorRateAccent = errorRate === null
        ? 'text-slate-100'
        : errorRateSlo === null
            ? errorRate > 0 ? 'text-rose-300' : 'text-emerald-300'
            : errorRate > errorRateSlo ? 'text-rose-300' : 'text-emerald-300';
    const totalErrorsAccent = displayedFailedRequests === null
        ? 'text-slate-100'
        : displayedFailedRequests > 0 ? 'text-rose-300' : 'text-emerald-300';
    const trace = task?.prompt?.trace || {};
    const replayStartSeconds = numberAt(trace, ['start_seconds']) ?? 0;
    const configuredReplayEndSeconds = numberAt(trace, ['end_seconds']);
    const replayDurationSeconds = numberAt(task?.simulation || {}, ['duration_seconds']);
    const replayScaleFactor = numberAt(trace, ['synthesis_speedup_ratio']) ?? numberAt(task || {}, ['scale_factor']) ?? 1;
    const replayEndSeconds = configuredReplayEndSeconds
        ?? (replayDurationSeconds !== null
            ? replayStartSeconds + replayDurationSeconds * replayScaleFactor
            : null);
    const tokenizer = task?.prompt?.dataset?.tokenizer ?? task?.tokenizer;
    const statusForChip = taskStatusChip(taskStatus);
    const completionTimeline = useMemo(() => (
        Array.isArray(summary?.completion_timeline)
            ? summary.completion_timeline.map((point) => ({ ...point, time_seconds: point.end_seconds }))
            : []
    ), [summary?.completion_timeline]);
    const latencyTimeline = useMemo(() => (
        Array.isArray(summary?.latency_timeline)
            ? summary.latency_timeline.map((point) => ({ ...point, time_seconds: point.end_seconds }))
            : []
    ), [summary?.latency_timeline]);
    const ttftTimeline = useMemo(() => (
        Array.isArray(summary?.ttft_timeline)
            ? summary.ttft_timeline.map((point) => ({ ...point, time_seconds: point.end_seconds }))
            : []
    ), [summary?.ttft_timeline]);
    const tpotTimeline = useMemo(() => (
        Array.isArray(summary?.tpot_timeline)
            ? summary.tpot_timeline.map((point) => ({ ...point, time_seconds: point.end_seconds }))
            : []
    ), [summary?.tpot_timeline]);
    const ttftHeatmap = summary?.ttft_heatmap;
    const tpotHeatmap = summary?.tpot_heatmap;
    const throughputTimeline = useMemo(() => (
        Array.isArray(summary?.throughput_timeline)
            ? summary.throughput_timeline.map((point) => ({ ...point, time_seconds: point.end_seconds }))
            : []
    ), [summary?.throughput_timeline]);
    const goodputTimeline = useMemo(() => (
        Array.isArray(summary?.goodput_timeline)
            ? summary.goodput_timeline.map((point) => ({ ...point, time_seconds: point.end_seconds }))
            : []
    ), [summary?.goodput_timeline]);
    const errorTimeline = useMemo(() => (
        Array.isArray(summary?.error_timeline)
            ? summary.error_timeline.map((point) => ({ ...point, time_seconds: point.end_seconds }))
            : []
    ), [summary?.error_timeline]);
    const statusCodeBreakdown = Array.isArray(summary?.status_code_breakdown)
        ? summary.status_code_breakdown
        : [];

    return (
        <section className="mx-auto flex w-full min-w-0 max-w-[1560px] flex-col gap-4 px-4 py-5 text-slate-100 sm:px-6">
            <header className="grid gap-4 border-b border-slate-800/70 pb-5 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start">
                <div className="flex min-w-0 flex-1 items-start gap-3">
                    <button title="Back to simulations" onClick={() => onNavigate('optimization-simulate')} className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 bg-slate-900 text-slate-300 hover:border-cyan-500 hover:text-cyan-200"><ArrowLeft className="h-4 w-4" /></button>
                    <div className="min-w-0">
                        <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-cyan-400">Simulation</p>
                        <h1 className="mt-1 break-words text-2xl font-semibold tracking-tight">{task?.name || routeTaskId || 'Simulation task details'}</h1>
                        {task?.description && <p className="mt-2 text-sm text-slate-400">{task.description}</p>}
                        {task && (
                            <>
                                <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
                                    <StatusChip status={statusForChip} label={displayName(taskStatus)} pulse={taskStatus === 'running'} />
                                </div>
                                <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-slate-500">
                                    <div className="flex min-w-0 gap-2"><dt>Task ID</dt><dd className="break-all font-mono text-slate-400">{taskId}</dd></div>
                                    <div className="flex gap-2"><dt>Created</dt><dd className="text-slate-400">{formatTimestamp(task.created_at)}</dd></div>
                                </dl>
                            </>
                        )}
                    </div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                    <Button variant="outline" size="icon" onClick={() => loadTaskDetails()} disabled={loading} title="Refresh" aria-label="Refresh simulation task details">
                        <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                    </Button>
                    {task && RUNNING_STATUSES.has(taskStatus) && (
                        <Button
                            variant="dangerOutline"
                            onClick={stopTask}
                            disabled={!STOPPABLE_STATUSES.has(taskStatus)}
                            isLoading={stopping && stoppingTaskId === taskId}
                        >
                            <Square className="h-3.5 w-3.5" /> Stop
                        </Button>
                    )}
                    {task && !RUNNING_STATUSES.has(taskStatus) && (
                        <Button variant="secondary" onClick={rerunSimulationTask} isLoading={rerunningTaskId === taskId}>
                            <RefreshCw className="h-3.5 w-3.5" /> Rerun
                        </Button>
                    )}
                    {task && (
                        <Button
                            variant="dangerOutline"
                            onClick={deleteSimulationTask}
                            disabled={RUNNING_STATUSES.has(taskStatus)}
                            isLoading={deletingTaskId === taskId}
                            title={RUNNING_STATUSES.has(taskStatus) ? 'Stop this task before deleting it' : 'Delete task and all backend data'}
                        >
                            <Trash2 className="h-3.5 w-3.5" /> Delete
                        </Button>
                    )}
                </div>
            </header>

            {error && <div role="alert" className="flex gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200"><AlertTriangle className="h-4 w-4 shrink-0" />{error}</div>}
            {actionError && <div role="alert" className="flex gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200"><AlertTriangle className="h-4 w-4 shrink-0" />{actionError}</div>}

            {loading && !task ? (
                <p className="text-sm text-slate-500">Loading simulation task details...</p>
            ) : !task ? (
                <EmptyState icon={<Activity className="h-8 w-8" />} title="Task not found" message="Return to the simulation list and choose another task." />
            ) : (
                <div className="space-y-5">
                    <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
                        <div className="flex flex-wrap items-center gap-x-6 gap-y-3 text-xs">
                            {[
                                ['Model', task.model_name || '—'],
                                ['Endpoint', task.endpoint_url || '—'],
                                ['Backend', `${displayName(taskBackend(task))}${backendVersion ? ` ${backendVersion}` : ''}`],
                                ['Dataset', taskDataset(task)],
                                ['Scenario', scenarioDisplayName(task.scenario) || '—'],
                                ['Created', formatTimestamp(task.created_at)],
                            ].map(([label, value]) => (
                                <div key={label} className="min-w-0">
                                    <span className="mr-2 text-slate-500">{label}:</span>
                                    <span className="break-all font-semibold text-slate-200">{value}</span>
                                </div>
                            ))}
                            <div className="ml-auto flex items-center gap-2">
                                {RUNNING_STATUSES.has(taskStatus) && <Spinner />}
                                <StatusChip status={statusForChip} label={displayName(taskStatus)} pulse={taskStatus === 'running'} />
                            </div>
                        </div>
                        <div className="mt-4">
                            <div className="mb-1.5 flex justify-between gap-4 text-[10px] text-slate-400">
                                <span>{task.progress_message || task.message || displayName(taskStatus)}</span>
                                <span>{formatNumber(progress, '%')}</span>
                            </div>
                            <div className="h-2 overflow-hidden rounded-full bg-slate-800" role="progressbar" aria-valuenow={progress} aria-valuemin="0" aria-valuemax="100">
                                <div className="h-full rounded-full bg-gradient-to-r from-emerald-500 to-cyan-400 transition-all" style={{ width: `${progress}%` }} />
                            </div>
                        </div>
                    </section>

                    <nav className="flex overflow-x-auto border-b border-slate-800" role="tablist" aria-label="Task detail sections">
                        {[
                            ['summary', 'Summary', Info],
                            ['latency', 'Latency', Clock3],
                            ['throughput', 'Throughput', Gauge],
                            ['errors', 'Errors & Logs', AlertTriangle],
                        ]
                            .filter(([key]) => taskStatus !== 'running' || !['latency', 'throughput'].includes(key))
                            .map(([key, label, Icon]) => (
                                <button
                                    key={key}
                                    type="button"
                                    role="tab"
                                    aria-selected={detailTab === key}
                                    onClick={() => setDetailTab(key)}
                                    className={`relative flex shrink-0 items-center gap-2 px-5 py-3 text-xs font-semibold transition-colors ${detailTab === key ? 'text-cyan-300' : 'text-slate-500 hover:text-slate-300'}`}
                                >
                                    {React.createElement(Icon, { className: 'h-3.5 w-3.5' })}
                                    {label}
                                    {key === 'errors' && (task.error_message || Number(failedRequests) > 0) && <span className="h-1.5 w-1.5 rounded-full bg-rose-400" />}
                                    {detailTab === key && <span className="absolute inset-x-2 bottom-0 h-0.5 rounded-full bg-cyan-400" />}
                                </button>
                            ))}
                    </nav>

                    {resultWarnings.length > 0 && (
                        <section className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4" aria-label="Result warnings">
                            {resultWarnings.map((warning) => (
                                <p key={warning} className="flex items-start gap-2 text-xs text-amber-100">
                                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" />
                                    {warning}
                                </p>
                            ))}
                        </section>
                    )}

                    {detailTab === 'errors' && (taskStatus === 'failed' || task.error_message) && (
                        <section className="rounded-xl border-2 border-rose-500/40 bg-rose-500/10 p-5" aria-labelledby="simulation-failure-heading">
                            <div className="flex items-start gap-3">
                                <AlertTriangle className="mt-0.5 h-6 w-6 shrink-0 text-rose-400" />
                                <div className="min-w-0 flex-1">
                                    <h3 id="simulation-failure-heading" className="text-lg font-bold text-rose-200">Task Failed</h3>
                                    <p className="mt-1 text-xs text-rose-200/80">{task.progress_message || 'The simulation encountered an error during execution.'}</p>
                                    <div className="mt-4 rounded-lg border border-rose-500/20 bg-slate-950/80 p-4 font-mono text-[11px] leading-relaxed text-rose-100">
                                        <div><span className="text-slate-500">Error: </span>{task.error_message || 'No error message was returned.'}</div>
                                        <div><span className="text-slate-500">Progress: </span>{task.progress_message || '—'}</div>
                                        <div className="mt-2"><span className="text-slate-500">Created: </span>{formatTimestamp(task.created_at)}</div>
                                        <div><span className="text-slate-500">Started: </span>{formatTimestamp(task.started_at)}</div>
                                        <div><span className="text-slate-500">Failed: </span>{formatTimestamp(task.completed_at)}</div>
                                        <div className="mt-2"><span className="text-slate-500">Scenario / backend: </span>{scenarioDisplayName(task.scenario) || '—'} / {displayName(taskBackend(task))}</div>
                                        <div><span className="text-slate-500">Endpoint / model: </span>{task.endpoint_url || '—'} / {task.model_name || '—'}</div>
                                        <div><span className="text-slate-500">Trace: </span>{trace.path || task.trace_path || '—'} ({trace.format || task.trace_format || 'unknown format'})</div>
                                    </div>
                                </div>
                            </div>
                        </section>
                    )}

                    {detailTab === 'summary' && <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Task overview">
                        <DetailCard label="Duration" value={formatDuration(task, summary, durationClockMs)} />
                        <DetailCard label="Error Rate" value={formatNumber(errorRate, '%')} accent={errorRateAccent} detail={errorRateSlo === null ? undefined : `SLA ≤ ${formatNumber(errorRateSlo, '%')}`} />
                        <DetailCard label="Total Requests" value={formatCount(displayedTotalRequests)} />
                        <DetailCard label="Total Errors" value={formatCount(displayedFailedRequests)} accent={totalErrorsAccent} />
                    </section>}

                    {detailTab === 'summary' && <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Key performance metrics">
                        <DetailCard label="Average TTFT" value={formatMilliseconds(ttftMean)} accent={ttftAccent} />
                        <DetailCard label="Average TPOT" value={formatMilliseconds(tpotMean)} accent={tpotAccent} />
                        <DetailCard label="Request Throughput" value={formatNumber(requestRate, ' req/s')} />
                        <DetailCard label="Output Token Throughput" value={formatNumber(tokenRate, ' tok/s')} />
                    </section>}

                    {detailTab === 'summary' && <Panel title="Request Processing Timeline">
                        {completionTimeline.length > 0 ? (
                            <>
                                <div className="mb-3 flex flex-wrap gap-4 text-[10px] text-slate-400">
                                    <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 border-t-2 border-blue-400 bg-blue-400/20" />Arrived per interval</span>
                                    <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 border-t-2 border-emerald-400 bg-emerald-400/20" />Successful per interval</span>
                                    <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 border-t-2 border-rose-400 bg-rose-400/20" />Failed per interval</span>
                                    <span className="flex items-center gap-1.5"><span className="h-0.5 w-3 bg-violet-400" />Cumulative completed</span>
                                </div>
                                <div className="h-72 w-full">
                                    <ResponsiveContainer width="100%" height="100%">
                                        <ComposedChart data={completionTimeline} margin={{ top: 8, right: 20, left: 0, bottom: 4 }}>
                                            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                                            <XAxis dataKey="time_seconds" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(value) => `${formatNumber(value)}s`} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" />
                                            <YAxis yAxisId="interval" allowDecimals={false} width={48} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" />
                                            <YAxis yAxisId="cumulative" orientation="right" allowDecimals={false} width={56} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" />
                                            <Tooltip labelFormatter={(value) => `Elapsed ${formatNumber(value)}s`} formatter={(value, name) => [formatNumber(value), name]} contentStyle={{ background: '#020617', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }} />
                                            <Area yAxisId="interval" type="monotone" dataKey="arrived_requests" name="Arrived requests" stroke="#60a5fa" strokeWidth={2} fill="#60a5fa" fillOpacity={0.18} dot={false} isAnimationActive={false} />
                                            <Area yAxisId="interval" type="monotone" dataKey="successful_requests" name="Successful requests" stroke="#34d399" strokeWidth={2} fill="#34d399" fillOpacity={0.18} dot={false} isAnimationActive={false} />
                                            <Area yAxisId="interval" type="monotone" dataKey="failed_requests" name="Failed requests" stroke="#fb7185" strokeWidth={2} fill="#fb7185" fillOpacity={0.18} dot={false} isAnimationActive={false} />
                                            <Line yAxisId="cumulative" type="monotone" dataKey="cumulative_completed" name="Cumulative completed" stroke="#a78bfa" strokeWidth={2} dot={false} isAnimationActive={false} />
                                        </ComposedChart>
                                    </ResponsiveContainer>
                                </div>
                            </>
                        ) : (
                            <p className="py-12 text-center text-xs text-slate-500">{RUNNING_STATUSES.has(taskStatus) ? 'Request processing data will appear as requests complete.' : 'No request arrival or completion timestamps are available.'}</p>
                        )}
                    </Panel>}

                    {detailTab === 'throughput' && <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" aria-label="Throughput highlights">
                        <div className="rounded-xl border border-blue-400/20 bg-gradient-to-br from-blue-500/80 to-blue-700/80 p-5 text-white shadow-lg"><div className="flex items-center justify-between text-xs text-blue-100"><span>Request Throughput</span><Gauge className="h-5 w-5" /></div><div className="mt-2 text-3xl font-bold">{formatNumber(requestRate)}</div><div className="text-xs text-blue-100/80">requests/second</div></div>
                        <div className="rounded-xl border border-amber-400/20 bg-gradient-to-br from-amber-500/80 to-amber-700/80 p-5 text-white shadow-lg"><div className="flex items-center justify-between text-xs text-amber-100"><span>Output Token Throughput</span><Activity className="h-5 w-5" /></div><div className="mt-2 text-3xl font-bold">{formatNumber(tokenRate)}</div><div className="text-xs text-amber-100/80">tokens/second</div></div>
                        <div className="rounded-xl border border-cyan-400/20 bg-gradient-to-br from-cyan-500/80 to-cyan-700/80 p-5 text-white shadow-lg"><div className="flex items-center justify-between text-xs text-cyan-100"><span>Input Token Throughput</span><Activity className="h-5 w-5" /></div><div className="mt-2 text-3xl font-bold">{formatNumber(inputTokenRate)}</div><div className="text-xs text-cyan-100/80">tokens/second</div></div>
                        <div className="rounded-xl border border-violet-400/20 bg-gradient-to-br from-violet-500/80 to-violet-700/80 p-5 text-white shadow-lg"><div className="flex items-center justify-between text-xs text-violet-100"><span>Total Token Throughput</span><Activity className="h-5 w-5" /></div><div className="mt-2 text-3xl font-bold">{formatNumber(totalTokenRate)}</div><div className="text-xs text-violet-100/80">tokens/second</div></div>
                    </section>}

                    {detailTab === 'throughput' && <Panel title="Throughput over Time">
                        {throughputTimeline.length > 0 ? (
                            <>
                                <div className="mb-3 flex flex-wrap gap-4 text-[10px] text-slate-400">
                                    <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 border-t-2 border-blue-400 bg-blue-400/20" />Request arrival rate</span>
                                    <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 border-t-2 border-cyan-400 bg-cyan-400/20" />Request completion throughput</span>
                                    <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 border-t-2 border-amber-400 bg-amber-400/20" />Token completion throughput</span>
                                </div>
                                <div className="h-72 w-full">
                                    <ResponsiveContainer width="100%" height="100%">
                                        <ComposedChart data={throughputTimeline} margin={{ top: 8, right: 20, left: 0, bottom: 4 }}>
                                            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                                            <XAxis dataKey="time_seconds" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(value) => `${formatNumber(value)}s`} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" />
                                            <YAxis yAxisId="requests" width={56} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" label={{ value: 'req/s', angle: -90, position: 'insideLeft', fill: '#64748b', fontSize: 10 }} />
                                            <YAxis yAxisId="tokens" orientation="right" width={64} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" label={{ value: 'tok/s', angle: 90, position: 'insideRight', fill: '#64748b', fontSize: 10 }} />
                                            <Tooltip labelFormatter={(value) => `Elapsed ${formatNumber(value)}s`} formatter={(value, name) => [formatNumber(value, name === 'Token completion throughput' ? ' tok/s' : ' req/s'), name]} contentStyle={{ background: '#020617', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }} />
                                            <Area yAxisId="requests" type="monotone" dataKey="request_arrival_rps" name="Request arrival rate" stroke="#60a5fa" strokeWidth={2} fill="#60a5fa" fillOpacity={0.14} dot={false} isAnimationActive={false} />
                                            <Area yAxisId="requests" type="monotone" dataKey="request_completion_rps" name="Request completion throughput" stroke="#22d3ee" strokeWidth={2} fill="#22d3ee" fillOpacity={0.14} dot={false} isAnimationActive={false} />
                                            <Area yAxisId="tokens" type="monotone" dataKey="token_throughput_tps" name="Token completion throughput" stroke="#fbbf24" strokeWidth={2} fill="#fbbf24" fillOpacity={0.12} dot={false} isAnimationActive={false} />
                                        </ComposedChart>
                                    </ResponsiveContainer>
                                </div>
                            </>
                        ) : <p className="py-12 text-center text-xs text-slate-500">No per-request throughput timestamps are available.</p>}
                    </Panel>}

                    {detailTab === 'throughput' && <Panel title="Goodput over Time">
                        {goodputTimeline.length > 0 ? (
                            <>
                                <div className="mb-3 flex flex-wrap gap-4 text-[10px] text-slate-400">
                                    <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 border-t-2 border-cyan-400 bg-cyan-400/20" />Request completion throughput</span>
                                    <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 border-t-2 border-emerald-400 bg-emerald-400/20" />Goodput</span>
                                    <span>Successful requests{ttftSloMs !== null ? ` · TTFT ≤ ${formatNumber(ttftSloMs)} ms` : ''}{tpotSloMs !== null ? ` · TPOT ≤ ${formatNumber(tpotSloMs)} ms` : ''}</span>
                                </div>
                                <div className="h-72 w-full">
                                    <ResponsiveContainer width="100%" height="100%">
                                        <ComposedChart data={goodputTimeline} margin={{ top: 8, right: 20, left: 0, bottom: 4 }}>
                                            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                                            <XAxis dataKey="time_seconds" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(value) => `${formatNumber(value)}s`} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" />
                                            <YAxis width={56} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" label={{ value: 'req/s', angle: -90, position: 'insideLeft', fill: '#64748b', fontSize: 10 }} />
                                            <Tooltip labelFormatter={(value) => `Elapsed ${formatNumber(value)}s`} formatter={(value, name) => [formatNumber(value, ' req/s'), name]} contentStyle={{ background: '#020617', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }} />
                                            <Area type="monotone" dataKey="request_throughput_rps" name="Request completion throughput" stroke="#22d3ee" strokeWidth={2} fill="#22d3ee" fillOpacity={0.1} dot={false} isAnimationActive={false} />
                                            <Area type="monotone" dataKey="goodput_rps" name="Goodput" stroke="#34d399" strokeWidth={2} fill="#34d399" fillOpacity={0.2} dot={false} isAnimationActive={false} />
                                        </ComposedChart>
                                    </ResponsiveContainer>
                                </div>
                            </>
                        ) : <p className="py-12 text-center text-xs text-slate-500">No per-request goodput timestamps are available.</p>}
                    </Panel>}

                    {detailTab === 'latency' && <section className="grid gap-3 md:grid-cols-2" aria-label="Latency highlights"><div className="rounded-xl border border-violet-400/20 bg-gradient-to-br from-violet-500/80 to-violet-700/80 p-5 text-white shadow-lg"><div className="flex items-center justify-between text-xs text-violet-100"><span>Avg TTFT</span><Clock3 className="h-5 w-5" /></div><div className="mt-2 text-3xl font-bold">{formatMilliseconds(ttftMean)}</div></div><div className="rounded-xl border border-emerald-400/20 bg-gradient-to-br from-emerald-500/80 to-emerald-700/80 p-5 text-white shadow-lg"><div className="flex items-center justify-between text-xs text-emerald-100"><span>Avg TPOT</span><Activity className="h-5 w-5" /></div><div className="mt-2 text-3xl font-bold">{formatMilliseconds(tpotMean)}</div></div></section>}

                    {detailTab === 'latency' && <Panel title="Latency under Load">
                        {latencyTimeline.length > 0 ? (
                            <>
                                <div className="mb-3 flex flex-wrap gap-4 text-[10px] text-slate-400">
                                    <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 border-t-2 border-blue-400 bg-blue-400/20" />Request arrival rate</span>
                                    <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 border-t-2 border-amber-400 bg-amber-400/20" />Average latency</span>
                                    <span className="flex items-center gap-1.5"><span className="h-0.5 w-3 bg-rose-400" />P95 latency</span>
                                </div>
                                <div className="h-72 w-full">
                                    <ResponsiveContainer width="100%" height="100%">
                                        <ComposedChart data={latencyTimeline} margin={{ top: 8, right: 20, left: 0, bottom: 4 }}>
                                            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                                            <XAxis dataKey="time_seconds" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(value) => `${formatNumber(value)}s`} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" />
                                            <YAxis yAxisId="pressure" width={48} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" label={{ value: 'req/s', angle: -90, position: 'insideLeft', fill: '#64748b', fontSize: 10 }} />
                                            <YAxis yAxisId="latency" orientation="right" width={60} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" label={{ value: 'ms', angle: 90, position: 'insideRight', fill: '#64748b', fontSize: 10 }} />
                                            <Tooltip labelFormatter={(value) => `Elapsed ${formatNumber(value)}s`} formatter={(value, name) => [name.includes('latency') ? formatMilliseconds(value) : formatNumber(value, ' req/s'), name]} contentStyle={{ background: '#020617', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }} />
                                            <Area yAxisId="pressure" type="monotone" dataKey="request_arrival_rps" name="Request arrival rate" stroke="#60a5fa" strokeWidth={2} fill="#60a5fa" fillOpacity={0.16} dot={false} isAnimationActive={false} />
                                            <Area yAxisId="latency" type="monotone" dataKey="average_latency_ms" name="Average latency" stroke="#fbbf24" strokeWidth={2} fill="#fbbf24" fillOpacity={0.14} connectNulls dot={false} isAnimationActive={false} />
                                            <Line yAxisId="latency" type="monotone" dataKey="p95_latency_ms" name="P95 latency" stroke="#fb7185" strokeWidth={2} connectNulls dot={false} isAnimationActive={false} />
                                        </ComposedChart>
                                    </ResponsiveContainer>
                                </div>
                            </>
                        ) : <p className="py-12 text-center text-xs text-slate-500">No per-request latency timestamps are available.</p>}
                    </Panel>}

                    {detailTab === 'latency' && (<section className="grid gap-5 xl:grid-cols-2"><LatencyMetricTimeline title="TTFT over Time" metric="TTFT" data={ttftTimeline} color="#a78bfa" /><LatencyMetricTimeline title="TPOT over Time" metric="TPOT" data={tpotTimeline} color="#34d399" /><div className="xl:col-span-2"><LatencyHeatmap title="TTFT by Time and Input Sequence Length" metric="TTFT" data={ttftHeatmap} /></div><div className="xl:col-span-2"><LatencyHeatmap title="TPOT by Time and Output Sequence Length" metric="TPOT" data={tpotHeatmap} /></div></section>)}

                    {detailTab === 'throughput' && <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3" aria-label="Token and request statistics"><DetailCard label="Successful Requests" value={formatNumber(successfulRequests)} detail={`${formatNumber(successfulRequests)} / ${formatNumber(totalRequests)} successful / total${failedRequests !== null ? ` · ${formatNumber(failedRequests)} failed` : ''}`} /><DetailCard label="Total Output Tokens" value={formatNumber(totalOutputTokens)} detail={averageOutputTokens !== null ? `${formatNumber(averageOutputTokens)} average / successful request` : 'Average unavailable'} /><DetailCard label="Total Input Tokens" value={formatNumber(totalInputTokens)} detail={averageInputTokens !== null ? `${formatNumber(averageInputTokens)} average / successful request` : 'Average unavailable'} /></section>}

                    {detailTab === 'latency' && <section><Panel title="Latency Distribution"><MetricBars series={[{ label: 'End-to-end', values: latencyDistribution, tone: 'bg-blue-400' }, { label: 'TTFT', values: ttft, tone: 'bg-violet-400' }, { label: 'TPOT', values: tpot, tone: 'bg-emerald-400' }, { label: 'Schedule drift', values: driftDistribution, tone: 'bg-amber-400' }]} />{timing && <details className="mt-4"><summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-300">Raw timing metrics</summary><div className="mt-2"><JsonBlock value={timing} /></div></details>}</Panel></section>}

                    {detailTab === 'throughput' && <section><Panel title="Throughput Metrics"><div className="space-y-3">{[['Request throughput', requestRate, 'req/s', 'bg-blue-400'], ['Output token throughput', tokenRate, 'tok/s', 'bg-amber-400'], ['Input token throughput', inputTokenRate, 'tok/s', 'bg-cyan-400'], ['Total token throughput', totalTokenRate, 'tok/s', 'bg-violet-400'], ['Effective concurrency', effectiveConcurrency, 'requests', 'bg-fuchsia-400'], ['Error rate', errorRate, '%', errorRateAccent === 'text-rose-300' ? 'bg-rose-400' : 'bg-emerald-400']].map(([label, value, unit, tone]) => (<div key={label} className="grid grid-cols-[8rem_1fr_auto] items-center gap-3 text-[11px]"><span className="text-slate-400">{label}</span><div className="h-2 overflow-hidden rounded-full bg-slate-800"><div className={`h-full rounded-full ${tone}`} style={{ width: value === null ? '0%' : `${Math.max(3, Math.min(100, unit === '%' ? value : 65))}%` }} /></div><span className="min-w-16 text-right font-mono text-slate-300">{formatNumber(value, unit === '%' ? '%' : '')}{unit !== '%' && value !== null ? ` ${unit}` : ''}</span></div>))}</div></Panel></section>}

                    {detailTab === 'throughput' && <Panel title="Detailed Results"><div className="overflow-x-auto"><table className="w-full min-w-[950px] text-left text-xs"><thead><tr className="border-b border-slate-700 text-slate-500">{['Configuration / Backend', 'Requests', 'Req/s', 'Tokens/s', 'Error Rate', 'Input Tokens', 'Output Tokens', 'Total Tokens'].map((heading) => <th key={heading} className="pb-3 pr-5 font-semibold">{heading}</th>)}</tr></thead><tbody><tr className="text-slate-300"><td className="py-3 pr-5 font-semibold">{scenarioDisplayName(task.scenario)} · {displayName(taskBackend(task))}{backendVersion ? ` ${backendVersion}` : ''}</td><td className="py-3 pr-5 font-mono">{formatNumber(totalRequests)}</td>{[requestRate, tokenRate].map((value, index) => <td key={index} className="py-3 pr-5 font-mono">{formatNumber(value)}</td>)}<td className={`py-3 pr-5 font-mono ${errorRateAccent}`}>{formatNumber(errorRate, '%')}</td><td className="py-3 pr-5 font-mono">{formatNumber(totalInputTokens)}</td><td className="py-3 pr-5 font-mono">{formatNumber(totalOutputTokens)}</td><td className="py-3 pr-5 font-mono">{formatNumber(totalTokens)}</td></tr></tbody></table></div></Panel>}

                    {detailTab === 'latency' && <section className="grid gap-5 lg:grid-cols-2"><StatisticsPanel title="TTFT Statistics" values={ttft} /><StatisticsPanel title="TPOT Statistics" values={tpot} /></section>}

                    {detailTab === 'summary' && <Panel title="Task Configuration"><div className="grid gap-4 xl:grid-cols-2"><ConfigurationGroup title="Endpoint" items={[['Endpoint mode', task.endpoint_mode ? displayName(task.endpoint_mode) : 'External'], ...(String(task.endpoint_mode).toLowerCase() === 'deployment' ? [['Cluster', task.endpoint_cluster_name], ['Deployment', task.endpoint_deployment_name]] : []), ['Endpoint', task.endpoint_url], ['Model', task.model_name], ['Streaming', task.simulation?.stream === undefined ? null : task.simulation.stream ? 'Enabled' : 'Disabled']]} /><ConfigurationGroup title="Replay Workload" items={[['Scenario', scenarioDisplayName(task.scenario)], ['Dataset', taskDataset(task)], ['Trace path', trace.path || task.trace_path], ['Trace format', trace.format || task.trace_format], ['Replay range', replayEndSeconds !== null ? `${formatNumber(replayStartSeconds)}–${formatNumber(replayEndSeconds)} source seconds` : null], ['Duration', task.simulation?.duration_seconds !== undefined ? `${task.simulation.duration_seconds} seconds` : null], ['Scale factor', trace.synthesis_speedup_ratio ?? task.scale_factor]]} /><ConfigurationGroup title="Backend Runtime" items={[['Backend', displayName(taskBackend(task))], ['Backend version', backendVersion], ['Timeout', trace.timeout_seconds !== undefined ? `${trace.timeout_seconds} seconds` : null], ['Grace period', task.simulation?.grace_period_seconds !== undefined ? `${task.simulation.grace_period_seconds} seconds` : null], ['Tokenizer', tokenizer]]} /><ConfigurationGroup title="SLA Targets" items={[['TTFT', ttftSloMs === null ? 'Not set' : `${formatNumber(ttftSloMs)} ms`], ['TPOT', tpotSloMs === null ? 'Not set' : `${formatNumber(tpotSloMs)} ms`], ['Error rate', errorRateSlo === null ? 'Not set' : `${formatNumber(errorRateSlo)}%`]]} />{backendOptionEntries.length > 0 && <ConfigurationGroup title="Advanced Backend Options" items={backendOptionEntries.map(([key, value]) => [backendOptionLabel(key), backendOptionValue(key, value)])} />}</div></Panel>}

                    {detailTab === 'errors' && <Panel title="Errors over Time">{errorTimeline.length > 0 ? (<><div className="mb-3 flex flex-wrap gap-4 text-[10px] text-slate-400"><span className="flex items-center gap-1.5"><span className="h-2.5 w-3 border-t-2 border-rose-400 bg-rose-400/20" />Failed requests</span><span className="flex items-center gap-1.5"><span className="h-0.5 w-3 bg-amber-400" />Error rate</span></div><div className="h-48 w-full"><ResponsiveContainer width="100%" height="100%"><ComposedChart data={errorTimeline} margin={{ top: 8, right: 20, left: 0, bottom: 4 }}><CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} /><XAxis dataKey="time_seconds" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(value) => `${formatNumber(value)}s`} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" /><YAxis yAxisId="failures" allowDecimals={false} width={48} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" /><YAxis yAxisId="rate" orientation="right" domain={[0, 'auto']} width={56} tickFormatter={(value) => `${formatNumber(value)}%`} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" /><Tooltip labelFormatter={(value) => `Elapsed ${formatNumber(value)}s`} formatter={(value, name) => [formatNumber(value, name === 'Error rate' ? '%' : ''), name]} contentStyle={{ background: '#020617', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }} /><Area yAxisId="failures" type="monotone" dataKey="failed_requests" name="Failed requests" stroke="#fb7185" strokeWidth={2} fill="#fb7185" fillOpacity={0.18} dot={false} isAnimationActive={false} /><Line yAxisId="rate" type="monotone" dataKey="error_rate_percent" name="Error rate" stroke="#fbbf24" strokeWidth={2} dot={false} isAnimationActive={false} /></ComposedChart></ResponsiveContainer></div></>) : <p className="py-12 text-center text-xs text-slate-500">No per-request error timestamps are available.</p>}</Panel>}

                    {detailTab === 'errors' && <Panel title="Error Summary"><dl className="grid gap-4 text-xs sm:grid-cols-3"><div><dt className="text-slate-500">Failed requests</dt><dd className={`mt-1 text-xl font-bold ${Number(failedRequests) > 0 ? 'text-rose-300' : 'text-emerald-300'}`}>{formatNumber(failedRequests)}</dd></div><div><dt className="text-slate-500">Task error</dt><dd className="mt-1 text-slate-200">{task.error_message || 'None'}</dd></div><div><dt className="text-slate-500">Final state</dt><dd className="mt-1"><StatusChip status={statusForChip} label={displayName(taskStatus)} pulse={taskStatus === 'running'} /></dd></div></dl></Panel>}

                    {detailTab === 'errors' && <Panel title="Errors by Type">{statusCodeBreakdown.length > 0 ? <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{statusCodeBreakdown.map((item) => { const statusCode = item.status_code ?? null; return (<button type="button" key={statusCode ?? 'unknown'} onClick={() => void openResponseCodeIssues(statusCode)} className="rounded-lg border border-slate-800 bg-slate-950/40 p-4 text-left transition-colors hover:border-rose-500/50 hover:bg-rose-500/5 focus:outline-none focus:ring-2 focus:ring-rose-500/40"><div className="flex items-start justify-between gap-4"><div><div className="font-mono text-sm font-bold text-rose-300">{statusCode === null ? 'Client / transport' : `HTTP ${statusCode}`}</div><div className="mt-1 text-[10px] text-slate-500">{formatCount(item.count)} requests · {formatNumber(item.percentage, '%')} of errors</div></div><div className="rounded-md bg-rose-500/10 px-2 py-1 font-mono text-xs font-semibold text-rose-300">{formatCount(item.count)}</div></div><p className="mt-3 text-xs leading-relaxed text-slate-400">{responseStatusGuidance(statusCode)}</p><p className="mt-3 text-[10px] font-semibold text-rose-300">View request issues</p></button>); })}</div> : <p className="text-xs text-slate-500">No failed request details are available.</p>}</Panel>}

                    {detailTab === 'errors' && <Panel title="Task Log">{taskLogs.length > 0 ? <div ref={taskLogRef} className="max-h-72 overflow-auto rounded-lg border border-slate-800 bg-slate-950/70 p-3 font-mono text-[10px] leading-relaxed text-slate-400">{taskLogs.map((line, index) => <div key={`${index}-${typeof line === 'string' ? line : JSON.stringify(line)}`}>{typeof line === 'string' ? line : JSON.stringify(line)}</div>)}</div> : <p className="text-xs text-slate-500">No task log entries are available yet.</p>}</Panel>}

                    {detailTab === 'errors' && <Panel title="Artifacts">{artifacts.length ? (<>{artifactDownloadError && <p role="alert" className="mb-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-400">{artifactDownloadError}</p>}<ul className="grid gap-2 md:grid-cols-2">{artifacts.map((artifact, index) => (<li key={`${artifact.kind || artifact.name}-${index}`} className="flex items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3"><div className="min-w-0"><div className="flex items-center gap-2 text-xs font-semibold text-slate-200"><FileJson className="h-3.5 w-3.5 shrink-0 text-cyan-400" />{displayName(artifact.kind || artifact.name || `Artifact ${index + 1}`)}</div>{artifact.media_type && <p className="mt-1 text-[10px] text-slate-600">{artifact.media_type}</p>}</div><Button variant="secondary" size="sm" onClick={() => downloadArtifact(artifact)} disabled={downloadingArtifact === artifact.kind}><Download className="h-3.5 w-3.5" />{downloadingArtifact === artifact.kind ? 'Downloading…' : 'Download'}</Button></li>))}</ul></>) : <p className="text-xs text-slate-500">{TERMINAL_STATUSES.has(taskStatus) ? 'No artifacts were returned.' : 'Artifacts will appear when the backend produces them.'}</p>}<div className="mt-4 space-y-2 border-t border-slate-800 pt-4"><details><summary className="cursor-pointer text-xs font-semibold text-slate-400 hover:text-slate-200">Raw backend metrics</summary><div className="mt-3"><JsonBlock value={backendMetrics} /></div></details><details><summary className="cursor-pointer text-xs font-semibold text-slate-400 hover:text-slate-200">Raw normalized result</summary><div className="mt-3"><JsonBlock value={result} /></div></details></div></Panel>}
                </div>
            )}

            <Modal
                isOpen={selectedResponseCode !== undefined}
                onClose={closeResponseCodeIssues}
                title={selectedResponseCode === null ? 'Client / transport request issues' : `HTTP ${selectedResponseCode} request issues`}
                subtitle={`${formatCount(responseCodeIssueTotal)} matching failed requests`}
                size="xl"
            >
                {responseCodeIssuesLoading ? (
                    <div className="flex justify-center py-12"><Spinner /></div>
                ) : responseCodeIssuesError ? (
                    <div role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">{responseCodeIssuesError}</div>
                ) : responseCodeIssues.length > 0 ? (
                    <>
                        <div className="overflow-x-auto rounded-lg border border-slate-800">
                            <table className="w-full min-w-[760px] text-left text-xs">
                                <thead className="bg-slate-950/80 text-[10px] uppercase tracking-wide text-slate-500"><tr><th className="px-3 py-2">Request</th><th className="px-3 py-2">Start</th><th className="px-3 py-2">Latency</th><th className="px-3 py-2">Input / Output</th><th className="px-3 py-2">Issue</th></tr></thead>
                                <tbody className="divide-y divide-slate-800">
                                    {responseCodeIssues.map((issue) => (
                                        <tr key={`${issue.request_number}-${issue.request_id || ''}`} className="bg-slate-950/30 align-top">
                                            <td className="px-3 py-3 font-mono text-slate-300">{issue.request_id || `#${issue.request_number}`}</td>
                                            <td className="px-3 py-3 font-mono text-slate-400">{issue.started_at_seconds == null ? '—' : `${formatNumber(issue.started_at_seconds)}s`}</td>
                                            <td className="px-3 py-3 font-mono text-slate-400">{issue.latency_ms == null ? '—' : `${formatMilliseconds(issue.latency_ms)}`}</td>
                                            <td className="px-3 py-3 font-mono text-slate-400">{issue.input_tokens == null ? '—' : formatCount(issue.input_tokens)} {' / '} {issue.output_tokens == null ? '—' : formatCount(issue.output_tokens)}</td>
                                            <td className="max-w-md whitespace-pre-wrap break-words px-3 py-3 text-rose-200">{issue.error || responseStatusGuidance(issue.status_code)}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                        <div className="mt-4 flex items-center justify-between gap-3">
                            <p className="text-[10px] text-slate-500">{formatCount(responseCodeIssuePage * RESPONSE_CODE_ISSUE_PAGE_SIZE + 1)}–{formatCount(Math.min(responseCodeIssueTotal, responseCodeIssuePage * RESPONSE_CODE_ISSUE_PAGE_SIZE + responseCodeIssues.length))} of {formatCount(responseCodeIssueTotal)}</p>
                            <div className="flex gap-2">
                                <button type="button" disabled={responseCodeIssuePage === 0} onClick={() => void openResponseCodeIssues(selectedResponseCode, responseCodeIssuePage - 1)} className="inline-flex items-center gap-1 rounded-md border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-slate-500 disabled:cursor-not-allowed disabled:opacity-40"><ChevronLeft className="h-3.5 w-3.5" /> Previous</button>
                                <button type="button" disabled={(responseCodeIssuePage + 1) * RESPONSE_CODE_ISSUE_PAGE_SIZE >= responseCodeIssueTotal} onClick={() => void openResponseCodeIssues(selectedResponseCode, responseCodeIssuePage + 1)} className="inline-flex items-center gap-1 rounded-md border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-slate-500 disabled:cursor-not-allowed disabled:opacity-40">Next <ChevronRight className="h-3.5 w-3.5" /></button>
                            </div>
                        </div>
                    </>
                ) : (
                    <p className="py-12 text-center text-xs text-slate-500">No matching per-request issue records are available.</p>
                )}
            </Modal>
        </section>
    );
}
