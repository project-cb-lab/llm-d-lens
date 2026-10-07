import { downloadBlob } from '../utils/download';
import { evaluationApi as api } from '../features/evaluation/client';
import { TERMINAL_EVALUATION_STATUSES as terminalStatuses, evaluationStatusTone } from '../features/evaluation/domain';
import { usePolling } from '../hooks/usePolling';
import { confirmDelete } from "./ui/confirmDelete";
import { useCallback, useEffect, useMemo, useState } from "react";
import { historicalFlows } from "./benchmark-results/historicalFlow.js";
import { liveDeployments } from "./benchmark-results/deploymentEvidence.js";
import { benchmarkDetails } from "./benchmark-results/benchmarkDetails.js";
import BenchmarkLiveFlow from "./benchmark-results/BenchmarkLiveFlow.jsx";
import GuideResultExplorer from "./benchmark-results/GuideResultExplorer.jsx";
import { buildResultReport } from "./benchmark-results/resultReport.js";
import { resolveExplorerGuide } from "./benchmark-results/resultExplorer.js";
import { Activity, AlertTriangle, ArrowLeft, ChevronDown, ChevronLeft, ChevronRight, Cpu, Download, FileText, Gauge, RefreshCw, RotateCcw, Send, Server, Square, Terminal, Trash2 } from "lucide-react";

import { evaluationReportFilename } from "../utils/evaluationReport";
import { guideResultProfile } from "./benchmark-results/guideProfiles";

function comparisonName(type) {
    return {
        'direct-vllm': 'Prism plain vLLM reference',
        'router-neutral': 'Prism neutral-router reference',
        'load-only': 'Load-only',
        'affinity-only': 'Affinity Policy Only',
        'optimized-baseline': 'llm-d optimized-baseline Guide',
        'kubernetes-service': 'Kubernetes Service round-robin',
    }[type] || type;
}

function DiagnosticBlock({ title, value }) {
    if (!value) return null;
    return <div className="min-w-0 max-w-full border-t border-slate-800 pt-3"><h3 className="mb-2 flex items-center gap-2 text-xs font-semibold text-slate-300"><Terminal className="h-3.5 w-3.5 text-cyan-300" />{title}</h3><pre className="max-h-64 max-w-full overflow-auto bg-black/30 p-3 text-[11px] leading-5 text-slate-400">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre></div>;
}

function modelFromText(value) {
    const match = value && String(value).match(/(?:model_name|model)\s*[=:]\s*["']?([^,\s}']+)/i);
    return match ? match[1] : "";
}



function deploymentCases(details) {
    if (details.cases) {
        return details.cases.flatMap((entry) => {
            const deploymentRunId = entry.case.deployment_run_id;
            const cases = entry.deployment_cases || [];
            if (!cases.length) return [{
                id: entry.case.id,
                guide: entry.case.kind === "baseline" ? "baseline-vllm" : entry.case.configuration?.guide || entry.case.spec?.guide,
                status: entry.case.status,
                deploymentRunId,
                evaluationCase: entry.case,
                deploymentOwned: details.workflow.deployment_ownership !== "existing-endpoint" && entry.case.deployment_ownership !== "existing-endpoint",
                failure: entry.case.error ? { detail: entry.case.error } : undefined,
                pendingDeployment: !deploymentRunId && !entry.case.error,
            }];
            return cases.map((item) => ({
                ...item,
                deploymentStatus: item.status,
                status: item.status,
                deploymentRunId,
                evaluationCase: entry.case,
                deploymentOwned: details.workflow.deployment_ownership !== "existing-endpoint" && entry.case.deployment_ownership !== "existing-endpoint",
                usedByEvaluation: item.id === entry.case.deployment_case_id,
            }));
        });
    }
    return (details.deployment?.cases || []).map((item) => ({
        ...item,
        deploymentRunId: details.deployment.id,
        evaluationCase: details.workflow,
        deploymentOwned: details.workflow.deployment_ownership !== "existing-endpoint" && Boolean(details.workflow.deployment_run_id),
    }));
}

function extractBenchmarks(details) {
    const items = [];
    if (details.cases) {
        for (const entry of details.cases) {
            const caseMetrics = entry.case?.metrics || entry.evaluation?.metrics || {};
            const matrixResults = entry.case?.matrix_results || entry.evaluation?.matrix_results || [];
            const rateStageResults = entry.case?.rate_stage_results || entry.evaluation?.rate_stage_results || [];
            const spec = entry.case.configuration || entry.case.spec || {};
            const benchmark = entry.case.benchmark || spec.benchmark || {};
            const isBaseline = entry.case.kind === "baseline";
            const deploymentKind = isBaseline ? comparisonName(entry.case.baseline_type || "direct-vllm") : spec.guide;
            const deploymentLabel = spec.guide_variant ? `${deploymentKind} · ${spec.guide_variant}` : deploymentKind;
            const usedDeployment = (entry.deployment_cases || []).find((candidate) => candidate.id === entry.case.deployment_case_id)
                || entry.deployment_cases?.[0];
            const common = {
                id: entry.case.id,
                name: `${spec.model || "model"} · ${deploymentLabel || "guide"}`.trim(),
                model: spec.model || entry.case.model || entry.evaluation?.model || modelFromText(entry.evaluation?.harness_logs || entry.evaluation?.stdout),
                runtime: spec.runtime,
                accelerator: spec.accelerator,
                guide: deploymentLabel,
                policyId: entry.case.baseline_type || spec.guide,
                guideVariant: spec.guide_variant,
                kind: entry.case.kind,
                replicas: spec.decode_replicas || spec.replicas,
                tp: spec.decode_tensor_parallel_size || spec.tensor_parallel_size,
                prefillReplicas: spec.prefill_replicas,
                prefillTp: spec.prefill_tensor_parallel_size,
                decodeReplicas: spec.decode_replicas,
                decodeTp: spec.decode_tensor_parallel_size,
                parallelism: benchmark.parallelism,
                workload: benchmark.workload,
                endpointKind: entry.case.endpoint_kind || (isBaseline && entry.case.baseline_type === "direct-vllm" ? "direct" : "routed"),
                resources: usedDeployment?.resource_snapshot,
            };
            if (rateStageResults.length) {
                for (const [stageIndex, stage] of rateStageResults.entries()) {
                    if (!stage.metrics) continue;
                    items.push({
                        ...common,
                        id: `${entry.case.id}:rate-stage-${stageIndex}`,
                        name: `${common.name} · ${stage.rate} req/s offered`,
                        offeredRate: stage.rate,
                        parallelism: stage.rate,
                        metrics: { ...caseMetrics, ...stage.metrics },
                    });
                }
            } else if (matrixResults.length) {
                for (const [pointIndex, point] of matrixResults.entries()) {
                    const stages = point.stage_metrics?.length ? point.stage_metrics : [{ metrics: point.metrics }];
                    for (const [stageIndex, stage] of stages.entries()) {
                        if (!stage.metrics) continue;
                        const dimensions = [`ISL ${point.isl}`, `OSL ${point.osl}`];
                        if (stage.concurrency != null) dimensions.push(`C ${stage.concurrency}`);
                        items.push({
                            ...common,
                            id: `${entry.case.id}:matrix-${pointIndex}:stage-${stageIndex}`,
                            name: `${common.name} · ${dimensions.join(" · ")}`,
                            parallelism: stage.concurrency ?? common.parallelism,
                            metrics: { ...caseMetrics, ...point.metrics, ...stage.metrics },
                        });
                    }
                }
            } else if (Object.keys(caseMetrics).length) {
                items.push({ ...common, metrics: caseMetrics });
            }
        }
    } else if (details.evaluation?.metrics) {
        const workflow = details.workflow || {};
        items.push({
            id: details.evaluation.id || workflow.id,
            name: `${workflow.model || "model"} · ${workflow.harness || ""}`.trim(),
            model: workflow.model,
            guide: workflow.harness,
            replicas: workflow.replicas,
            tp: workflow.tensor_parallel_size,
            metrics: details.evaluation.metrics,
        });
    }
    return items;
}

function resolveGuideType(details) {
    return resolveExplorerGuide(details);
}

export default function OptimizationEvaluationDetails({ onNavigate }) {
    const params = new URLSearchParams(window.location.search);
    const evaluationId = params.get("evaluationId") || "";
    const runKind = params.get("runKind") || "workflow";
    const [details, setDetails] = useState(null);
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(true);
    const [logs, setLogs] = useState({});
    const [expandedLogs, setExpandedLogs] = useState({});
    const [caseActions, setCaseActions] = useState({});
    const [caseActionErrors, setCaseActionErrors] = useState({});
    const [retryTokens, setRetryTokens] = useState({});
    const [requestedSection, setActiveSection] = useState("overview");
    const [caseStatusFilter, setCaseStatusFilter] = useState("all");
    const [showCaseHistory, setShowCaseHistory] = useState(false);
    const [casePage, setCasePage] = useState(1);

    const refreshDeployments = useCallback(async (currentDetails) => {
        const refreshes = deploymentCases(currentDetails)
            .filter((item) => item.deploymentOwned && item.deploymentRunId && item.execution_id && !terminalStatuses.has(item.evaluationCase?.status))
            .map((item) => api(
                `/api/v1/deployments/runs/${encodeURIComponent(item.deploymentRunId)}/cases/${encodeURIComponent(item.id)}/refresh`,
                { method: "POST" },
            ));
        await Promise.allSettled(refreshes);
    }, []);

    const loadDetails = useCallback(async ({ refreshDeployments: shouldRefreshDeployments = false, quiet = false } = {}) => {
        if (!evaluationId) {
            setError("An evaluation ID is required.");
            setLoading(false);
            return;
        }
        if (!quiet) setLoading(true);
        try {
            let nextDetails = runKind === "benchmark"
                ? await api(`/api/v1/evaluate/runs/${encodeURIComponent(evaluationId)}`).then(benchmarkDetails)
                : await api(`/api/v1/evaluate/workflow-runs/${encodeURIComponent(evaluationId)}/details`);
            if (shouldRefreshDeployments) {
                await refreshDeployments(nextDetails);
                nextDetails = runKind === "benchmark"
                    ? await api(`/api/v1/evaluate/runs/${encodeURIComponent(evaluationId)}`).then(benchmarkDetails)
                    : await api(`/api/v1/evaluate/workflow-runs/${encodeURIComponent(evaluationId)}/details`);
            }
            setDetails(nextDetails);
            setError("");
        } catch (nextError) {
            setError(nextError.message);
        } finally {
            setLoading(false);
        }
    }, [evaluationId, runKind, refreshDeployments]);

    useEffect(() => { loadDetails(); }, [loadDetails]);
    const workflowStatus = details?.workflow.status;
    const standalone = runKind === "benchmark";

    usePolling(() => loadDetails({ quiet: true }), {
        enabled: Boolean(workflowStatus) && !terminalStatuses.has(workflowStatus),
    });

    const loadLogs = async (item) => {
        if (!item.deploymentRunId || !item.execution_id) return;
        const key = `${item.deploymentRunId}:${item.id}`;
        try {
            setLogs((current) => ({ ...current, [key]: { loading: true } }));
            const payload = await api(`/api/v1/deployments/runs/${encodeURIComponent(item.deploymentRunId)}/cases/${encodeURIComponent(item.id)}/logs`);
            setLogs((current) => ({ ...current, [key]: payload }));
        } catch (nextError) {
            setLogs((current) => ({ ...current, [key]: { error: nextError.message } }));
        }
    };

    const cases = useMemo(() => details ? deploymentCases(details) : [], [details]);
    const hasLiveDeployment = liveDeployments(cases).some(item => item.liveAvailable);
    const hasMonitoring = hasLiveDeployment || historicalFlows(details).length > 0;
    const activeSection = requestedSection === "live" && !hasMonitoring ? "overview" : requestedSection;
    const benchmarks = useMemo(() => details ? extractBenchmarks(details) : [], [details]);
    const guideType = useMemo(() => resolveGuideType(details), [details]);
    const guideProfile = useMemo(() => guideResultProfile(guideType), [guideType]);
    const caseSummary = useMemo(() => ({
        failed: cases.filter((item) => ["failed", "cancelled"].includes(item.status)).length,
        active: cases.filter((item) => ["queued", "rendering", "deploying", "running", "benchmarking"].includes(item.status)).length,
        ready: cases.filter((item) => ["ready", "succeeded"].includes(item.status)).length,
    }), [cases]);
    const visibleCases = useMemo(() => {
        const latestAttempts = new Map();
        cases.forEach((item) => {
            const family = `${item.deploymentRunId}:${item.parent_case_id || item.id}`;
            const current = latestAttempts.get(family);
            if (!current || (item.attempt || 0) >= (current.attempt || 0)) latestAttempts.set(family, item);
        });
        const source = showCaseHistory ? cases : [...latestAttempts.values()];
        return source.filter((item) => {
            if (caseStatusFilter === "failed") return ["failed", "cancelled"].includes(item.status);
            if (caseStatusFilter === "active") return ["queued", "rendering", "deploying", "running", "benchmarking"].includes(item.status);
            if (caseStatusFilter === "ready") return ["ready", "succeeded"].includes(item.status);
            return true;
        });
    }, [cases, caseStatusFilter, showCaseHistory]);
    const casePageSize = 6;
    const casePageCount = Math.max(1, Math.ceil(visibleCases.length / casePageSize));
    const pagedCases = visibleCases.slice((casePage - 1) * casePageSize, casePage * casePageSize);
    useEffect(() => { setCasePage(1); }, [caseStatusFilter, showCaseHistory]);
    useEffect(() => { if (casePage > casePageCount) setCasePage(casePageCount); }, [casePage, casePageCount]);
    const sendToSimulate = () => {
        if (!benchmarks.length) return;
        sessionStorage.setItem("prism_simulate_benchmarks", JSON.stringify(benchmarks));
        onNavigate("optimization-simulate");
    };
    const downloadReport = () => {
        if (!details) return;
        const markdown = buildResultReport(details, guideType);
        downloadBlob(new Blob([markdown], { type: "text/markdown;charset=utf-8" }), evaluationReportFilename(details));
    };
    const toggleLogs = (item) => {
        const key = `${item.deploymentRunId}:${item.id}`;
        setExpandedLogs((current) => ({ ...current, [key]: !current[key] }));
        if (!logs[key]) loadLogs(item);
    };

    const runCaseAction = async (item, action) => {
        if (action === "cancel") {
            const key = `${item.deploymentRunId}:${item.id}`;
            setCaseActions(current => ({ ...current, [key]: action }));
            setCaseActionErrors(current => ({ ...current, [key]: "" }));
            try {
                const endpoint = standalone
                    ? `/api/v1/evaluate/runs/${encodeURIComponent(evaluationId)}/cancel`
                    : details.cases
                    ? `/api/v1/evaluate/workflow-runs/${encodeURIComponent(evaluationId)}/cases/${encodeURIComponent(item.evaluationCase.id)}/cancel`
                    : details.workflow.deployment_ownership === 'existing-endpoint'
                        ? `/api/v1/evaluate/runs/${encodeURIComponent(details.evaluation?.id || evaluationId)}/cancel`
                        : `/api/v1/evaluate/workflow-runs/${encodeURIComponent(evaluationId)}/cancel`;
                await api(endpoint, { method: 'POST' });
            } catch (error) {
                setCaseActionErrors(current => ({ ...current, [key]: error.message }));
            } finally {
                await loadDetails();
                setCaseActions(current => ({ ...current, [key]: "" }));
            }
            return;
        }
        if (!item.deploymentRunId || !item.deploymentOwned) return;
        if (action === "delete" && !await confirmDelete(`Delete deployment ${item.id} and its Kubernetes namespace? This cannot be undone.`)) return;
        const key = `${item.deploymentRunId}:${item.id}`;
        setCaseActions((current) => ({ ...current, [key]: action }));
        setCaseActionErrors((current) => ({ ...current, [key]: "" }));
        try {
            const clusterSessionId = sessionStorage.getItem("prism_cluster_session_id");
            if (clusterSessionId) {
                await api(`/api/v1/deployments/runs/${encodeURIComponent(item.deploymentRunId)}/cluster-session`, {
                    method: "POST",
                    body: JSON.stringify({ cluster_session_id: clusterSessionId }),
                });
            }
            const endpoint = `/api/v1/deployments/runs/${encodeURIComponent(item.deploymentRunId)}/cases/${encodeURIComponent(item.id)}/${action === "delete" ? "clean" : action}`;
            const modelToken = action === "restart" ? (retryTokens[key] || "").trim() : "";
            await api(endpoint, {
                method: "POST",
                body: action === "delete"
                    ? JSON.stringify({ preserve_rendered_overlay: false })
                    : action === "restart" ? JSON.stringify({ model_token: modelToken || null }) : undefined,
            });
            if (modelToken) setRetryTokens((current) => ({ ...current, [key]: "" }));
            await loadDetails();
        } catch (nextError) {
            setCaseActionErrors((current) => ({ ...current, [key]: nextError.message }));
        } finally {
            setCaseActions((current) => ({ ...current, [key]: "" }));
        }
    };
    return <section className="mx-auto flex w-full min-w-0 max-w-[1560px] flex-col gap-4 px-4 py-5 text-slate-100 sm:px-6">
        <header className="grid gap-4 border-b border-slate-800/70 pb-5 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start">
            <div className="flex min-w-0 flex-1 items-start gap-3">
                <button title="Back to evaluations" onClick={() => onNavigate("optimization-evaluate")} className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 bg-slate-900 text-slate-300 hover:border-cyan-500 hover:text-cyan-200"><ArrowLeft className="h-4 w-4" /></button>
                <div className="min-w-0"><p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-cyan-400">Benchmark</p><h1 className="mt-1 break-words text-2xl font-semibold tracking-tight">{details?.workflow.name || details?.workflow.id || evaluationId || "Benchmark details"}</h1>{details && <><div className="mt-3 flex flex-wrap items-center gap-3 text-xs"><span className={`rounded-full border px-2.5 py-1 ${evaluationStatusTone(details.workflow.status)}`}>{details.workflow.status}</span><span className="text-slate-400">{details.cases?.length ?? cases.length} cases</span><span className="text-slate-500">{guideProfile.label}</span></div><dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-slate-500"><div className="flex min-w-0 gap-2"><dt>Task ID</dt><dd className="break-all font-mono text-slate-400">{details.workflow.id}</dd></div><div className="flex gap-2"><dt>Created</dt><dd className="text-slate-400">{details.workflow.created_at || '—'}</dd></div></dl></>}</div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
                <button title="Download the complete benchmark report" disabled={!details} onClick={downloadReport} className="inline-flex h-9 items-center gap-2 rounded-lg border border-cyan-500/40 bg-cyan-500/10 px-3 text-xs font-semibold text-cyan-100 hover:border-cyan-300 disabled:opacity-40"><Download className="h-4 w-4" /> Download Report</button>
                <button title="Send benchmark results to Simulate & TCO" disabled={!benchmarks.length} onClick={sendToSimulate} className="inline-flex h-9 items-center gap-2 rounded-lg border border-emerald-500/40 bg-emerald-500/5 px-3 text-xs text-emerald-200 hover:border-emerald-400 disabled:opacity-40"><Send className="h-4 w-4" /> Send to Simulate &amp; TCO</button>
                <button title="Refresh deployment status and details" onClick={() => loadDetails({ refreshDeployments: true })} className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 bg-slate-900 text-slate-300 hover:border-cyan-500 hover:text-cyan-200"><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /></button>
            </div>
        </header>
        {details?.workflow?.endpoint_used && <div className="rounded-xl border border-cyan-500/20 bg-cyan-500/[0.04] p-3 text-[10px] text-slate-400">
            <div className="flex flex-wrap items-center justify-between gap-2"><span className="font-semibold uppercase tracking-wider text-cyan-300">Actual benchmark target</span><span>{details.workflow.endpoint_preflight?.status === "reachable" ? `Reachable (HTTP ${details.workflow.endpoint_preflight.http_status})` : "Target recorded by benchmark runner"}</span></div>
            <p className="mt-1 break-all font-mono text-slate-300">{details.workflow.endpoint_used}</p>
            {details.workflow.workload_summary && <p className="mt-2">{details.workflow.workload_summary.matrix_points || 0} matrix points · {(details.workflow.workload_summary.concurrency_stages || []).length} concurrency stages · {details.workflow.workload_summary.parallelism ?? 1} worker(s) · timeout {details.workflow.workload_summary.wait_timeout_seconds ?? "—"}s</p>}
        </div>}
        {details?.workflow && <div className="grid gap-3 rounded-xl border border-slate-800/80 bg-slate-900/45 p-3 text-[10px] text-slate-500 sm:grid-cols-2 lg:grid-cols-4">
            <div><span className="block uppercase tracking-wider text-slate-600">Phase</span><b className="mt-1 block text-sm text-cyan-200">{details.workflow.phase || details.workflow.status || "—"}</b></div>
            <div title={details.workflow.phase_started_at || details.workflow.started_at || ""}><span className="block uppercase tracking-wider text-slate-600">Started</span><b className="mt-1 block truncate text-slate-300">{details.workflow.phase_started_at || details.workflow.started_at || "—"}</b></div>
            <div title="Heartbeat shows runner liveness; it does not represent request progress"><span className="block uppercase tracking-wider text-slate-600">Runner heartbeat</span><b className="mt-1 block truncate text-slate-300">{details.workflow.heartbeat_at || "—"}</b></div>
            <div title={details.workflow.last_log_at || ""}><span className="block uppercase tracking-wider text-slate-600">Last output</span><b className="mt-1 block truncate text-slate-300">{details.workflow.last_log_at || "—"}</b></div>
        </div>}
        <nav aria-label="Benchmark detail sections" className="sticky top-0 z-30 flex gap-0 overflow-x-auto rounded-xl border border-slate-800/70 bg-[#080d17]/95 p-1 shadow-xl backdrop-blur-xl">{[
            ["overview", "Benchmark results", <Gauge key="overview-icon" className="h-3.5 w-3.5" />],
            ["compare", "Compare", <Gauge key="compare-icon" className="h-3.5 w-3.5" />],
            ["live", hasLiveDeployment ? "Live monitoring" : "Recorded monitoring", <Activity key="live-icon" className="h-3.5 w-3.5" />],
            ["resources", "Resources", <Cpu key="resources-icon" className="h-3.5 w-3.5" />],
            ["logs", "Benchmark logs", <Activity key="logs-icon" className="h-3.5 w-3.5" />],
            ["deployments", "Deployments", <Server key="deployments-icon" className="h-3.5 w-3.5" />],
        ].filter(([key]) => key !== "live" || hasMonitoring).map(([key, label, icon]) => <button key={key} aria-current={activeSection === key ? "page" : undefined} onClick={() => setActiveSection(key)} className={`inline-flex h-9 min-w-max items-center gap-2 rounded-lg px-3 text-[11px] font-semibold transition ${activeSection === key ? "bg-cyan-500/10 text-cyan-300 shadow-[inset_0_0_0_1px_rgba(34,211,238,.25)]" : "text-slate-500 hover:bg-slate-800/60 hover:text-slate-300"}`}>{icon}{label}</button>)}</nav>
        {error && <div className="flex gap-2 border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200"><AlertTriangle className="h-4 w-4" />{error}</div>}
        {loading && !details ? <p className="text-sm text-slate-500">Loading evaluation details...</p> : details && <>

            {details.workflow.cancellation_error && <div role="alert" className="border border-amber-500/30 p-3 text-sm text-amber-200">{details.workflow.cancellation_error}</div>}
            {details.workflow.error && <div role="alert" className="border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">{details.workflow.error}</div>}
            {activeSection === "logs" && <div className="space-y-3">
            {(standalone ? [details.workflow] : (details.cases || []).map(entry => entry.evaluation).filter(Boolean)).map(run => <section key={run.id} aria-label="Benchmark pod diagnostics">
                {!error && !details.workflow.error && (run.error || run.harness_warning) && <div role="alert" className="border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">{run.error || run.harness_warning}</div>}
                <div className="my-3 space-y-3 text-xs text-slate-400">
                    {run.harness_logs && <DiagnosticBlock title="Harness pod logs" value={run.harness_logs} />}
                    <DiagnosticBlock title="Benchmark output" value={run.stdout || 'No runner output received yet.'} />
                    {run.stderr && <DiagnosticBlock title="Benchmark diagnostics" value={run.stderr} />}
                </div>
            </section>)}
            </div>}
            <div hidden={["deployments", "live", "logs"].includes(activeSection)}><GuideResultExplorer key={evaluationId} details={details} guideType={guideType} view={["deployments", "live", "logs"].includes(activeSection) ? "overview" : activeSection} onViewChange={setActiveSection} /></div>
            {activeSection === "deployments" && <section className="min-w-0 space-y-4">
                <div className="rounded-2xl border border-slate-800/80 bg-[#090e18] p-4 shadow-xl"><div className="flex flex-col gap-4 xl:flex-row xl:items-center xl:justify-between"><div><h2 className="flex items-center gap-2 text-sm font-semibold"><Server className="h-4 w-4 text-emerald-300" />Deployment cases</h2><p className="mt-1 text-[10px] text-slate-500">Latest attempts · Open a case’s logs to inspect deployment progress.</p></div><div className="flex flex-wrap items-center gap-2">{[["all", "All", cases.length, "text-slate-300"], ["failed", "Failed", caseSummary.failed, "text-red-300"], ["active", "Active", caseSummary.active, "text-cyan-300"], ["ready", "Ready", caseSummary.ready, "text-emerald-300"]].map(([key, label, count, color]) => <button key={key} onClick={() => setCaseStatusFilter(key)} className={`inline-flex h-8 items-center gap-2 rounded-lg border px-3 text-[10px] font-semibold transition ${caseStatusFilter === key ? "border-cyan-500/35 bg-cyan-500/10" : "border-slate-800 bg-slate-950 hover:border-slate-700"} ${color}`}>{label}<span className="rounded bg-black/25 px-1.5 py-0.5">{count}</span></button>)}<button title={showCaseHistory ? "Hide older retry attempts and keep only the latest attempt" : "Include older retry attempts for each deployment case"} onClick={() => setShowCaseHistory((current) => !current)} className={`h-8 rounded-lg border px-3 text-[10px] ${showCaseHistory ? "border-violet-500/35 bg-violet-500/10 text-violet-200" : "border-slate-800 bg-slate-950 text-slate-400"}`}>{showCaseHistory ? "Hide history" : "Retry history"}</button></div></div></div>
                {visibleCases.length ? pagedCases.map((item) => {
                    const key = `${item.deploymentRunId}:${item.id}`;
                    const diagnostics = item.diagnostics?.value || {};
                    const snapshot = diagnostics.snapshot || diagnostics;
                    const log = logs[key];
                    const readiness = diagnostics.readiness || diagnostics.reasons || diagnostics.reason;
                    const logsExpanded = expandedLogs[key];
                    const actionInProgress = caseActions[key];
                    const actionError = caseActionErrors[key];
                    const hasQueuedRetry = cases.some((candidate) => candidate.deploymentRunId === item.deploymentRunId
                        && (candidate.parent_case_id || candidate.id) === (item.parent_case_id || item.id)
                        && candidate.attempt > item.attempt
                        && ["queued", "rendering", "deploying"].includes(candidate.status));
                    const isFailed = ["failed", "cancelled"].includes(item.status);
                    return <article key={key} className={`relative overflow-hidden rounded-xl border p-4 shadow-lg ${isFailed ? "border-red-500/45 bg-gradient-to-r from-red-950/35 via-[#0b0d15] to-[#0b0d15] shadow-red-950/20" : "border-slate-800 bg-[#090e18]"}`}>
                        {isFailed && <span className="absolute inset-y-0 left-0 w-1 bg-red-500" />}
                        <div className="flex flex-wrap items-center gap-2">{isFailed && <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-red-500/15 text-red-300"><AlertTriangle className="h-4 w-4" /></span>}<b className="min-w-0 flex-1 truncate text-sm">{item.guide || item.id}</b>{Number.isInteger(item.attempt) && <span className="rounded border border-slate-600 bg-slate-900 px-2 py-0.5 text-[10px] text-slate-300">Attempt {item.attempt}</span>}<span className="text-xs text-slate-400">Benchmark: {item.evaluationCase?.status} · {item.deploymentOwned ? "Evaluation-created service" : "Existing endpoint · service retained"}</span>{item.usedByEvaluation && <span className="rounded border border-cyan-500/40 bg-cyan-500/10 px-2 py-0.5 text-[10px] text-cyan-200">Used by this evaluation</span>}<span className={`rounded border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${evaluationStatusTone(item.status)}`}>{item.status}</span>{item.deploymentStatus && item.deploymentStatus !== item.status && <span className="rounded border border-slate-700 px-2 py-0.5 text-[10px] text-slate-400">Lifecycle {item.deploymentStatus}</span>}</div>
                        <div className="mt-3 grid gap-3 text-xs text-slate-400 md:grid-cols-3"><div><span className="text-slate-600">Namespace</span><p className="mt-1 break-all font-mono text-slate-300">{item.namespace || "-"}</p></div><div><span className="text-slate-600">Endpoint</span><p className="mt-1 break-all font-mono text-slate-300">{item.endpoint || "-"}</p></div><div><span className="text-slate-600">Execution</span><p className="mt-1 font-mono text-slate-300">{item.execution_status || "-"}</p></div></div>
                        {item.status === "failed" && item.failure?.code === "model_access_failed" && <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3"><label className="text-[9px] font-bold uppercase tracking-wider text-amber-300" htmlFor={`model-token-${key}`}>Hugging Face token for this retry</label><input id={`model-token-${key}`} type="password" autoComplete="off" value={retryTokens[key] || ""} onChange={(event) => setRetryTokens((current) => ({ ...current, [key]: event.target.value }))} placeholder="hf_…" className="mt-2 h-9 w-full border border-amber-500/30 bg-slate-950 px-3 text-xs text-slate-100 outline-none focus:border-amber-400" /><p className="mt-2 text-[10px] leading-4 text-amber-100/70">Optional. Use only for a private or gated model. The token is sent to this deployment namespace for the next attempt and is not saved in the configuration artifact or retry history.</p></div>}
                        {item.pendingDeployment ? <p className="mt-4 border-t border-slate-800 pt-3 text-xs text-amber-200">This deployment has not started because the preceding baseline case did not complete successfully.</p> : <div className="mt-4 flex flex-wrap gap-2 border-t border-slate-800 pt-3">{item.execution_id ? <button onClick={() => toggleLogs(item)} className="inline-flex h-8 items-center gap-2 border border-cyan-500/40 px-3 text-xs text-cyan-200"><FileText className="h-3.5 w-3.5" />{log?.loading ? "Loading logs..." : "Deploy logs"}{logsExpanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}</button> : <span className="inline-flex h-8 items-center border border-slate-700 px-3 text-xs text-slate-500">Starting deployment...</span>}{hasQueuedRetry && <span className="inline-flex h-8 items-center border border-cyan-500/40 px-3 text-xs text-cyan-200">Retry attempt is pending</span>}{item.deploymentOwned && item.status === "failed" && !hasQueuedRetry && <button title="Retry deployment" disabled={actionInProgress} onClick={() => runCaseAction(item, "restart")} className="inline-flex h-8 items-center gap-2 border border-emerald-500/40 px-3 text-xs text-emerald-200"><RotateCcw className="h-3.5 w-3.5" />Retry</button>}{item.deploymentOwned && item.status === "stopped" && !hasQueuedRetry && <button title="Redeploy this paused service" disabled={actionInProgress} onClick={() => runCaseAction(item, "restart")} className="inline-flex h-8 items-center gap-2 border border-emerald-500/40 px-3 text-xs text-emerald-200"><RotateCcw className="h-3.5 w-3.5" />Resume</button>}{(!terminalStatuses.has(item.evaluationCase?.status) || (item.deploymentOwned && ['ready','deploying','rendering','queued'].includes(item.status))) && <button title={item.deploymentOwned ? 'Cancel benchmark and its Evaluation-created deployment' : 'Cancel benchmark only; the existing service keeps running'} disabled={actionInProgress} onClick={() => runCaseAction(item, "cancel")} className="inline-flex h-8 items-center gap-2 border border-amber-500/40 px-3 text-xs text-amber-100"><Square className="h-3.5 w-3.5" />{actionInProgress === 'cancel' ? 'Cancelling…' : item.deploymentOwned ? 'Cancel benchmark & deployment' : 'Cancel benchmark'}</button>}{item.deploymentOwned && ["failed", "stopped", "cleaned", "cleaned_up"].includes(item.status) && <button title="Delete deployment and namespace" disabled={actionInProgress} onClick={() => runCaseAction(item, "delete")} className="inline-flex h-8 items-center gap-2 border border-red-500/40 px-3 text-xs text-red-200"><Trash2 className="h-3.5 w-3.5" />Delete</button>}</div>}{actionError && <p className="mt-3 border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-200">Deployment action failed: {actionError}</p>}
                        {logsExpanded && <><>{log?.error && <div className="mt-3 flex items-center justify-between gap-3 border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-100"><span>Log retrieval failed: {log.error}</span><button onClick={() => loadLogs(item)} className="border border-amber-400/40 px-2 py-1 text-amber-100">Retry</button></div>}</>{log && !log.error && <DiagnosticBlock title="Deploy and Kubernetes logs" value={log.entries?.map((entry) => entry.message).join("\n") || "No deployment log entries are available yet."} />}<div className="mt-4 grid gap-4 xl:grid-cols-3"><DiagnosticBlock title="Kubernetes pods" value={snapshot.pods} /><DiagnosticBlock title="Kubernetes events" value={snapshot.events} /><DiagnosticBlock title="Modelserver logs" value={snapshot.modelserver_logs} /></div>{readiness && <DiagnosticBlock title="Latest readiness check" value={readiness} />}</>}
                    </article>;
                }) : <p className="rounded-xl border border-slate-800 bg-slate-950/40 p-8 text-center text-sm text-slate-500">{cases.length ? "No deployment cases match this filter." : "No deployment cases are available for this evaluation."}</p>}
                {visibleCases.length > casePageSize && <div className="flex items-center justify-between border-t border-slate-800/60 pt-3"><span className="text-[10px] text-slate-500">Showing {(casePage - 1) * casePageSize + 1}–{Math.min(casePage * casePageSize, visibleCases.length)} of {visibleCases.length}</span><div className="flex items-center gap-1"><button disabled={casePage === 1} onClick={() => setCasePage((page) => page - 1)} className="inline-flex h-8 items-center gap-1 rounded-lg border border-slate-800 px-2.5 text-[10px] text-slate-300 disabled:opacity-30"><ChevronLeft className="h-3.5 w-3.5" />Previous</button><span className="min-w-16 text-center text-[10px] text-slate-400">{casePage} / {casePageCount}</span><button disabled={casePage === casePageCount} onClick={() => setCasePage((page) => page + 1)} className="inline-flex h-8 items-center gap-1 rounded-lg border border-slate-800 px-2.5 text-[10px] text-slate-300 disabled:opacity-30">Next<ChevronRight className="h-3.5 w-3.5" /></button></div></div>}

            </section>}
            {activeSection === "live" && <BenchmarkLiveFlow cases={cases} details={details} />}
        </>}
    </section>;
}
