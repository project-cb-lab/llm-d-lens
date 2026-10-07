import { requestJson } from '../api/httpClient';
import { useAuth } from '../features/auth/useAuth';
import { confirmDelete } from "./ui/confirmDelete";
import { useEffect, useState } from "react";
import {
    Activity,
    AlertTriangle,
    ChevronRight,
    Clock3,
    Plus,
    Play,
    RefreshCw,
    RotateCcw,
    Server,
    Square,
    Trash2,
    X,
} from "lucide-react";
import { loadConfigurationArtifacts } from "./OptimizationWorkspace/configurationBackend";
import { loadCluster } from "./OptimizationWorkspace/clusterBackend";
import { getDeploymentExecutions } from "./OptimizationWorkspace/remoteDeployBackend";
import { TERMINAL_EVALUATION_STATUSES, evaluationStatusLabel, evaluationStatusTone } from "../features/evaluation/domain";
import { clearEvaluationIntent, readEvaluationArtifact, readEvaluationIntent, readEvaluationPlans, storeEvaluationArtifact, storeEvaluationIntent } from "../features/evaluation/transfer";

const inputClass =
    "mt-1 h-9 w-full border border-slate-700 bg-slate-950 px-2 text-xs text-slate-100 outline-none focus:border-cyan-500";
const DEFAULT_LOAD_LEVELS = [1, 2, 4, 8, 16];
const defaultRuntime = {
    http_proxy: "",
    https_proxy: "",
    no_proxy: "",
};
const newWorkload = (id) => ({
    id,
    configuration_artifact_id: "",
    include_baseline: true,
    preserve_deployment: false,
    benchmark: {
        harness: "inference-perf",
        workload: "sanity_random.yaml",
        parallelism: 1,
        wait_timeout_seconds: 1800,
        accelerator_profile: null,
        storage_class_name: null,
    },
});

const defaultLoadSweep = (workload) => DEFAULT_LOAD_LEVELS.map((parallelism, index) => ({
    ...workload,
    id: index === 0 ? workload.id : `${workload.id}-load-${parallelism}`,
    benchmark: { ...workload.benchmark, parallelism },
}));

const ensureDefaultLoadSweep = (items) => {
    if (items.length !== 1 || Number(items[0].benchmark?.parallelism || 1) !== 1) return items;
    return defaultLoadSweep(items[0]);
};

function api(path, options = {}) {
    return requestJson(path, { headers: { 'Content-Type': 'application/json' }, ...options });
}

function Metric({ label, value, suffix = "" }) {
    return (
        <div className="border border-slate-800 p-3 text-xs text-slate-500">
            {label}
            <b className="mt-1 block text-base text-slate-100">
                {value ?? "-"}
                {value != null ? suffix : ""}
            </b>
        </div>
    );
}

function WorkloadEditor({ value, index, artifact, onChange, onDuplicate, onRemove }) {
    const updateBenchmark = (patch) =>
        onChange({ benchmark: { ...value.benchmark, ...patch } });
    const configuration = artifact?.deployable_configuration;
    const content = configuration?.content || {};
    const decode = content.decode || content.serving || {};
    const prefill = content.prefill || {};
    return (
        <article className="border border-slate-800 bg-slate-950/40 p-4">
            <div className="mb-3 flex items-center justify-between">
                <div>
                    <b className="text-xs">Workload {index + 1}</b>
                    <p className={`mt-1 font-mono text-[10px] ${value.configuration_artifact_id ? 'text-emerald-300' : 'text-amber-300'}`}>
                        Configuration artifact: {value.configuration_artifact_id || 'not published'}
                    </p>
                </div>
                <div className="flex items-center gap-3">
                    <button type="button" onClick={onDuplicate} className="text-[10px] text-cyan-300 hover:text-cyan-100">Duplicate for load sweep</button>
                    {onRemove && <button type="button" onClick={onRemove}><X className="h-4 w-4 text-slate-500" /></button>}
                </div>
            </div>
            {configuration && (
                <div className="mb-4 grid gap-3 border border-slate-800 bg-slate-900/30 p-3 sm:grid-cols-2 lg:grid-cols-4">
                    <Metric label="Model" value={content.model?.name} />
                    <Metric label="Guide" value={configuration.provider_ref} />
                    <Metric label="Decode replicas / TP" value={`${decode.replicaCount ?? '-'} / ${decode.tensorParallelSize ?? '-'}`} />
                    <Metric label="Prefill replicas / TP" value={content.prefill ? `${prefill.replicaCount ?? '-'} / ${prefill.tensorParallelSize ?? '-'}` : 'N/A'} />
                </div>
            )}
            <div className="grid gap-3 sm:grid-cols-4">
                <label className="text-xs text-slate-400">
                    Benchmark plan ID
                    <input
                        className={inputClass}
                        value={value.id}
                        onChange={(event) =>
                            onChange({ id: event.target.value })
                        }
                    />
                </label>
                <label className="text-xs text-slate-400">
                    Harness
                    <select
                        className={inputClass}
                        value={value.benchmark.harness}
                        onChange={(event) => updateBenchmark({ harness: event.target.value })}
                    >
                        <option>inference-perf</option>
                        <option>guidellm</option>
                        <option>vllm-benchmark</option>
                    </select>
                </label>
                <label className="text-xs text-slate-400">
                    Benchmark workload profile
                    <input
                        className={inputClass}
                        value={value.benchmark.workload}
                        onChange={(event) =>
                            updateBenchmark({ workload: event.target.value })
                        }
                    />
                </label>
                <label className="text-xs text-slate-400">
                    Parallelism (offered load)
                    <input
                        type="number"
                        min="1"
                        max="128"
                        className={inputClass}
                        value={value.benchmark.parallelism}
                        onChange={(event) => updateBenchmark({ parallelism: Number(event.target.value) })}
                    />
                </label>
                <label className="text-xs text-slate-400">
                    Accelerator DRA profile
                    <input
                        className={inputClass}
                        value={value.benchmark.accelerator_profile || ""}
                        placeholder="Auto from Configuration"
                        onChange={(event) => updateBenchmark({ accelerator_profile: event.target.value || null })}
                    />
                </label>
                <label className="text-xs text-slate-400">
                    Workload StorageClass
                    <input
                        className={inputClass}
                        value={value.benchmark.storage_class_name || ""}
                        placeholder="Auto from cluster"
                        onChange={(event) => updateBenchmark({ storage_class_name: event.target.value || null })}
                    />
                </label>
                <label className="flex items-end gap-2 pb-2 text-xs">
                    <input
                        type="checkbox"
                        checked={value.include_baseline}
                        onChange={(event) =>
                            onChange({ include_baseline: event.target.checked })
                        }
                    />
                    Run deduplicated baseline
                </label>
                <label className="flex items-end gap-2 pb-2 text-xs">
                    <input
                        type="checkbox"
                        checked={value.preserve_deployment}
                        onChange={(event) =>
                            onChange({ preserve_deployment: event.target.checked })
                        }
                    />
                    Keep successful deployment for Monitoring
                </label>
            </div>
            <p className="mt-2 text-[10px] text-slate-500">Only request generation belongs here. Model, Guide, image, topology, mounts, and custom server parameters remain immutable in Configuration.</p>
        </article>
    );
}

function RunDetails({ details, onRefresh }) {
    const [logs, setLogs] = useState({});
    const loadLogs = async (entry, deploymentCase) => {
        const key = `${entry.case.id}:${deploymentCase.id}`;
        try {
            const payload = await api(
                `/api/v1/deployments/runs/${entry.case.deployment_run_id}/cases/${deploymentCase.id}/logs`,
            );
            setLogs((current) => ({ ...current, [key]: payload }));
        } catch (error) {
            setLogs((current) => ({
                ...current,
                [key]: { error: error.message },
            }));
        }
    };
    if (!details) return null;
    return (
        <section className="border border-slate-800 bg-slate-950/40 p-5">
            <div className="mb-4 flex justify-between">
                <h2 className="text-sm font-semibold">
                    Evaluation details ·{" "}
                    {details.workflow.name || details.workflow.id}
                </h2>
                <button onClick={onRefresh}>
                    <RefreshCw className="h-4 w-4" />
                </button>
            </div>
            {details.cases ? (
                <div className="space-y-4">
                    {details.cases.map((entry) => (
                        <article
                            key={entry.case.id}
                            className="border border-slate-800 p-4"
                        >
                            <div className="flex gap-2">
                                <b className="text-xs">
                                    {entry.case.kind} · {entry.case.configuration?.guide || entry.case.spec?.guide || "configuration artifact"}
                                </b>
                                <span
                                    className={`border px-2 py-0.5 text-[10px] ${evaluationStatusTone(entry.case.status)}`}
                                >
                                    {evaluationStatusLabel(entry.case.status)}
                                </span>
                            </div>
                            <div className="mt-3 grid grid-cols-2 gap-2 lg:grid-cols-4">
                                <Metric
                                    label="TPS"
                                    value={entry.case.metrics?.throughput_tps}
                                />
                                <Metric
                                    label="RPS"
                                    value={entry.case.metrics?.throughput_rps}
                                />
                                <Metric
                                    label="TTFT"
                                    value={entry.case.metrics?.ttft_ms}
                                    suffix=" ms"
                                />
                                <Metric
                                    label="TPOT"
                                    value={entry.case.metrics?.tpot_ms}
                                    suffix=" ms"
                                />
                            </div>
                            {entry.deployment_cases?.map((item) => {
                                const key = `${entry.case.id}:${item.id}`;
                                return (
                                    <div
                                        key={item.id}
                                        className="mt-3 border-t border-slate-800 pt-3 text-xs text-slate-500"
                                    >
                                        Deploy{" "}
                                        <span
                                            className={`border px-1.5 ${evaluationStatusTone(item.status)}`}
                                        >
                                            {evaluationStatusLabel(item.status)}
                                        </span>{" "}
                                        ·{" "}
                                        <span className="font-mono">
                                            {item.namespace || "-"}
                                        </span>{" "}
                                        ·{" "}
                                        <span className="font-mono">
                                            {item.endpoint || "-"}
                                        </span>
                                        <button
                                            onClick={() =>
                                                loadLogs(entry, item)
                                            }
                                            className="ml-2 text-cyan-300"
                                        >
                                            View logs
                                        </button>
                                        {logs[key] && (
                                            <pre className="mt-2 max-h-64 overflow-auto bg-black/30 p-2 text-[11px] text-slate-400">
                                                {logs[key].error ||
                                                    logs[key].entries
                                                        ?.map(
                                                            (line) =>
                                                                line.message,
                                                        )
                                                        .join("\n") ||
                                                    JSON.stringify(
                                                        logs[key],
                                                        null,
                                                        2,
                                                    )}
                                            </pre>
                                        )}
                                    </div>
                                );
                            })}
                            {entry.evaluation?.output && (
                                <p className="mt-2 break-all text-[11px] text-slate-500">
                                    Report root: {entry.evaluation.output}
                                </p>
                            )}
                        </article>
                    ))}
                    {details.report && (
                        <div>
                            <h3 className="mb-2 text-sm font-semibold">
                                Baseline comparison report
                            </h3>
                            {details.report.comparisons.map((item) => (
                                <div
                                    key={item.guide_case_id}
                                    className="mb-2 border border-slate-800 p-3 text-xs"
                                >
                                    <b>
                                        {item.workload_id} · {item.guide}
                                    </b>
                                    <pre className="mt-2 text-slate-400">
                                        {JSON.stringify(
                                            item.delta_percent,
                                            null,
                                            2,
                                        )}
                                    </pre>
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            ) : details.deployment ? (
                <div className="space-y-3">
                    <div className="flex gap-2 text-xs text-slate-500">
                        Deployment
                        <span className={`border px-1.5 ${evaluationStatusTone(details.deployment.status)}`}>
                            {evaluationStatusLabel(details.deployment.status)}
                        </span>
                        <span className="font-mono">{details.deployment.id}</span>
                    </div>
                    {details.deployment.cases.map((item) => (
                        <article key={item.id} className="border border-slate-800 p-4 text-xs">
                            <div className="flex gap-2">
                                <b className="min-w-0 flex-1 truncate">{item.guide || item.id}</b>
                                <span className={`border px-1.5 ${evaluationStatusTone(item.status)}`}>
                                    {evaluationStatusLabel(item.status)}
                                </span>
                            </div>
                            <p className="mt-2 font-mono text-[11px] text-slate-500">
                                {item.namespace || "-"} · {item.endpoint || "-"}
                            </p>
                            {item.diagnostics?.value?.reasons?.length > 0 && (
                                <pre className="mt-2 max-h-48 overflow-auto bg-black/30 p-2 text-[11px] text-slate-400">
                                    {item.diagnostics.value.reasons.join("\n")}
                                </pre>
                            )}
                        </article>
                    ))}
                </div>
            ) : (
                <p className="text-sm text-slate-500">No deployment details are available for this evaluation.</p>
            )}
        </section>
    );
}

export default function OptimizationEvaluateWorkspaceV2({ onNavigate }) {
    const { can } = useAuth();
    const canDesignEvaluation = can('evaluate:workflow:create');
    const canBenchmarkExisting = can('evaluate:run:create');
    const [pendingIntent] = useState(() => readEvaluationIntent());
    const [name, setName] = useState(pendingIntent?.name || "Evaluation");
    const [runtime, setRuntime] = useState(pendingIntent?.runtime || defaultRuntime);
    const [clusterRecord, setClusterRecord] = useState(null);
    const [prefilledCount] = useState(() => {
        const plans = readEvaluationPlans();
        return Array.isArray(plans) ? plans.length : 0;
    });
    const [prefillDismissed, setPrefillDismissed] = useState(false);
    const [workloads, setWorkloads] = useState(() => {
        const plans = readEvaluationPlans(undefined, { consume: true });
        if (Array.isArray(plans) && plans.length) {
            return ensureDefaultLoadSweep(plans.map((item, index) => ({
                        ...newWorkload(item.id || `workload-${index + 1}`),
                        configuration_artifact_id: item.configuration_artifact_id || "",
                        include_baseline: item.include_baseline ?? true,
                        preserve_deployment: item.preserve_deployment ?? pendingIntent?.preserve_deployment ?? false,
                        benchmark: { ...newWorkload(item.id || `workload-${index + 1}`).benchmark, ...item.benchmark },
                    })));
        }
        return defaultLoadSweep(newWorkload("workload-1"));
    });
    const [runs, setRuns] = useState([]);
    const [benchmarkRuns, setBenchmarkRuns] = useState([]);
    const [configurationArtifact, setConfigurationArtifact] = useState(() => readEvaluationArtifact());
    const [session, setSession] = useState(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [savedArtifacts, setSavedArtifacts] = useState([]);
    const [artifactsLoading, setArtifactsLoading] = useState(false);
    const sessionId = sessionStorage.getItem("prism_cluster_session_id") || "";

    // Mode B — benchmark an already-deployed service (no redeploy) via POST /runs.
    const [readyDeployments, setReadyDeployments] = useState([]);
    const [benchTargetId, setBenchTargetId] = useState("");
    const [benchParams, setBenchParams] = useState({
        harness: "inference-perf",
        workload: "sanity_random.yaml",
        parallelism: 1,
        accelerator_profile: "",
    });
    const [benchBusy, setBenchBusy] = useState(false);
    const [benchNotice, setBenchNotice] = useState("");

    const loadReadyDeployments = async () => {
        try {
            const items = await getDeploymentExecutions({ status: "ready" });
            setReadyDeployments(items);
            setBenchTargetId((current) =>
                items.some((item) => item.execution_id === current)
                    ? current
                    : items[0]?.execution_id || "",
            );
        } catch {
            // Non-fatal: the existing-deployment picker just stays empty.
        }
    };

    const loadSavedArtifacts = async () => {
        setArtifactsLoading(true);
        try {
            setSavedArtifacts(await loadConfigurationArtifacts());
        } catch (nextError) {
            setError(nextError.message);
        } finally {
            setArtifactsLoading(false);
        }
    };

    const selectSavedArtifact = (artifactId) => {
        const artifact = savedArtifacts.find((item) => item.artifact_id === artifactId) || null;
        setConfigurationArtifact(artifact);
        if (!artifact) return;
        setWorkloads((items) => items.map((item) => ({ ...item, configuration_artifact_id: artifact.artifact_id })));
        storeEvaluationArtifact(artifact);
    };

    const runExistingBenchmark = async () => {
        if (!benchTargetId) return;
        setBenchBusy(true);
        setError("");
        setBenchNotice("");
        try {
            const benchmark = await api("/api/v1/evaluate/runs", {
                method: "POST",
                body: JSON.stringify({
                    deployment_execution_id: benchTargetId,
                    harness: benchParams.harness,
                    workload: benchParams.workload,
                    parallelism: Number(benchParams.parallelism),
                    ...(benchParams.accelerator_profile ? { accelerator_profile: benchParams.accelerator_profile } : {}),
                }),
            });
            setBenchNotice(`Benchmark ${benchmark.id} started against the selected deployment.`);
            await loadBenchmarkRuns();
        } catch (nextError) {
            setError(nextError.message);
        } finally {
            setBenchBusy(false);
        }
    };

    const loadRuns = async () =>
        setRuns((await api("/api/v1/evaluate/workflow-runs")).items || []);
    const loadBenchmarkRuns = async () =>
        setBenchmarkRuns((await api("/api/v1/evaluate/runs")).items || []);
    useEffect(() => {
        Promise.all([
            loadRuns(),
            loadBenchmarkRuns(),
            loadReadyDeployments(),
            loadSavedArtifacts(),
            sessionId
                ? api(
                      `/api/cluster-overview/sessions/${encodeURIComponent(sessionId)}`,
                  ).then(async (nextSession) => {
                      setSession(nextSession);
                      setClusterRecord(await loadCluster(nextSession.serverId));
                  })
                : Promise.resolve(),
        ]).catch((nextError) => setError(nextError.message));
    }, [sessionId]);
    useEffect(() => {
        if (!runs.some((run) => !TERMINAL_EVALUATION_STATUSES.has(run.status)))
            return undefined;
        const timer = window.setInterval(() => {
            loadRuns();
            loadBenchmarkRuns();
            loadReadyDeployments();
        }, 5000);
        return () => window.clearInterval(timer);
    }, [runs]);

    const updateWorkload = (index, patch) =>
        setWorkloads((items) =>
            items.map((item, current) =>
                current === index ? { ...item, ...patch } : item,
            ),
        );
    const start = async () => {
        setBusy(true);
        setError("");
        try {
            if (!session) throw new Error("Connect a cluster before starting.");
            if (workloads.some((workload) => !workload.configuration_artifact_id) && !configurationArtifact) {
                storeEvaluationIntent({
                    operation: "create-evaluation",
                    return_target: "optimization-evaluate",
                    name,
                    runtime,
                    workloads,
                    created_at: new Date().toISOString(),
                });
                onNavigate("optimization-explore");
                return;
            }
            await api("/api/v1/evaluate/evaluations", {
                method: "POST",
                body: JSON.stringify({
                    name,
                    cluster_session_id: session.id,
                    runtime,
                    benchmark_plans: workloads.map((workload) => ({
                        id: workload.id,
                        configuration_artifact_id: workload.configuration_artifact_id || configurationArtifact?.artifact_id,
                        benchmark: workload.benchmark,
                        include_baseline: workload.include_baseline,
                        preserve_deployment: workload.preserve_deployment,
                    })),
                }),
            });
            clearEvaluationIntent();
            await loadRuns();
        } catch (nextError) {
            if (nextError.details?.code === "CONFIGURATION_REQUIRED") {
                storeEvaluationIntent({
                    name,
                    runtime,
                    workloads,
                });
                onNavigate(nextError.details.action?.target || "optimization-explore");
                return;
            }
            setError(nextError.message);
        } finally {
            setBusy(false);
        }
    };
    const action = async (run, name) => {
        if (name === "delete" && !await confirmDelete(`Delete benchmark ${run.id}? This cannot be undone.`)) return;
        try {
            await api(
                `/api/v1/evaluate/workflow-runs/${run.id}${name === "delete" ? "" : `/${name}`}`,
                { method: name === "delete" ? "DELETE" : "POST" },
            );
            await loadRuns();
        } catch (nextError) {
            setError(nextError.message);
        }
    };

    return (
        <section className="mx-auto flex max-w-[1500px] flex-col gap-5 px-5 py-7 text-slate-100">
            <header className="flex flex-wrap items-end justify-between gap-4 border-b border-slate-800 pb-5">
                <div>
                    <p className="text-xs font-semibold uppercase text-emerald-400">
                        Optimization
                    </p>
                    <h1 className="mt-1 text-2xl font-semibold">Evaluate</h1>
                    <p className="mt-2 text-sm text-slate-400">
                        Deploy Guides and deduplicated baselines sequentially,
                        then compare local llm-d-benchmark results.
                    </p>
                </div>
                <button
                    onClick={() => onNavigate("clusters")}
                    className="inline-flex h-9 items-center gap-2 border border-slate-700 px-3 text-xs"
                >
                    <Server className="h-4 w-4" />
                    {session
                        ? `${session.serverId} connected`
                        : "Connect cluster"}
                </button>
            </header>
            <div className={`flex items-center justify-between gap-3 border p-3 text-xs ${workloads.every((item) => item.configuration_artifact_id) || configurationArtifact ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-100" : "border-amber-500/30 bg-amber-500/10 text-amber-100"}`}>
                <span>
                    {workloads.every((item) => item.configuration_artifact_id)
                        ? `${workloads.length} workload deployment configuration artifact${workloads.length === 1 ? '' : 's'} ready.`
                        : configurationArtifact ? `Legacy deployment configuration ${configurationArtifact.artifact_id}` : "Each workload requires a published deployment configuration artifact."}
                </span>
                <button type="button" onClick={() => {
                    storeEvaluationIntent({
                        name,
                        runtime,
                        workloads,
                    });
                    onNavigate("optimization-explore");
                }} className="underline">
                    {configurationArtifact || workloads.some((item) => item.configuration_artifact_id) ? "Change configuration" : "Create configuration"}
                </button>
            </div>
            <section className="border border-slate-800 bg-slate-950/40 p-4">
                <div className="flex flex-wrap items-end gap-3">
                    <label className="min-w-[320px] flex-1 text-xs text-slate-400">
                        Reuse a previously saved deployment YAML
                        <select className={inputClass} disabled={artifactsLoading} value={configurationArtifact?.artifact_id || ""} onChange={(event) => selectSavedArtifact(event.target.value)}>
                            <option value="">Select a saved YAML artifact</option>
                            {savedArtifacts.map((artifact) => {
                                const configuration = artifact.deployable_configuration || {};
                                const cluster = configuration.provenance?.cluster_ref || {};
                                const modelName = configuration.content?.model?.name || configuration.type;
                                return <option key={artifact.artifact_id} value={artifact.artifact_id}>{modelName} · {configuration.provider_ref} · cluster {cluster.name || cluster.id || 'unknown'} · {new Date(artifact.created_at).toLocaleString()}</option>;
                            })}
                        </select>
                    </label>
                    <button type="button" onClick={loadSavedArtifacts} disabled={artifactsLoading} className="h-9 border border-slate-700 px-3 text-xs text-slate-300 disabled:opacity-40">{artifactsLoading ? "Loading…" : "Refresh YAML files"}</button>
                    {configurationArtifact && <a href={`/api/v1/configurations/artifacts/${configurationArtifact.artifact_id}/manifest`} download className="inline-flex h-9 items-center border border-cyan-500/50 px-3 text-xs text-cyan-200">Download YAML</a>}
                </div>
                {configurationArtifact && <p className="mt-2 text-[10px] text-slate-500">Bound target: {configurationArtifact.deployable_configuration?.provenance?.cluster_ref?.name || configurationArtifact.deployable_configuration?.provenance?.cluster_ref?.id} · session {configurationArtifact.deployable_configuration?.provenance?.cluster_ref?.session_id}. Deploy refuses a different cluster session.</p>}
            </section>
            {error && (
                <div className="flex gap-2 border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">
                    <AlertTriangle className="h-4 w-4" />
                    {error}
                </div>
            )}
            {prefilledCount > 0 && !prefillDismissed && (
                <div className="flex items-center justify-between gap-2 border border-cyan-500/30 bg-cyan-500/10 p-3 text-sm text-cyan-200">
                    <span>Prefilled {prefilledCount} workload{prefilledCount > 1 ? "s" : ""} from Configuration.</span>
                    <button type="button" onClick={() => setPrefillDismissed(true)} className="text-cyan-300 hover:text-cyan-100">
                        <X className="h-4 w-4" />
                    </button>
                </div>
            )}
            {!session && (
                <div className="flex flex-wrap items-center justify-between gap-3 border border-amber-500/30 bg-amber-500/10 p-4">
                    <div className="flex items-start gap-2">
                        <Server className="mt-0.5 h-4 w-4 text-amber-300" />
                        <div>
                            <p className="text-sm font-semibold text-amber-100">No cluster connected</p>
                            <p className="mt-0.5 text-xs text-amber-200/80">
                                Evaluate deploys and benchmarks on a connected cluster. Connect one in Cluster to enable deploy and benchmark.
                            </p>
                        </div>
                    </div>
                    <button type="button" onClick={() => onNavigate("clusters")} className="inline-flex h-9 items-center gap-2 border border-amber-500/50 bg-amber-500/10 px-3 text-xs text-amber-100 hover:border-amber-400">
                        <Server className="h-4 w-4" /> Connect cluster
                    </button>
                </div>
            )}
            <section className="border border-slate-800 bg-slate-950/40 p-4">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <div>
                        <h2 className="text-sm font-semibold">Benchmark an existing deployment</h2>
                        <p className="mt-0.5 text-xs text-slate-400">Run llm-d-benchmark against an already-deployed service — no redeploy.</p>
                    </div>
                    <button type="button" onClick={loadReadyDeployments} className="inline-flex h-8 items-center gap-2 border border-slate-700 px-2 text-xs text-slate-300">
                        <RefreshCw className="h-3.5 w-3.5" /> Refresh
                    </button>
                </div>
                {readyDeployments.length === 0 ? (
                    <div className="border border-dashed border-slate-700 bg-slate-900/30 p-3 text-xs text-slate-500">
                        No ready deployments yet. Start an evaluation below to deploy one, or generate a configuration first.
                    </div>
                ) : (
                    <div className="grid gap-3 md:grid-cols-2">
                        <div className="md:col-span-2">
                            <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Deployment</span>
                            <select className={inputClass} value={benchTargetId} onChange={(event) => setBenchTargetId(event.target.value)}>
                                {readyDeployments.map((item) => (
                                    <option key={item.execution_id} value={item.execution_id}>
                                        {item.model} · {item.namespace || "default"} · {item.forwarded_endpoint || item.endpoint}
                                    </option>
                                ))}
                            </select>
                        </div>
                        <div>
                            <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Harness</span>
                            <input className={inputClass} value={benchParams.harness} onChange={(event) => setBenchParams((previous) => ({ ...previous, harness: event.target.value }))} />
                        </div>
                        <div>
                            <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Benchmark workload profile</span>
                            <input className={inputClass} value={benchParams.workload} onChange={(event) => setBenchParams((previous) => ({ ...previous, workload: event.target.value }))} />
                            <p className="mt-1 text-[10px] text-slate-500">Controls inference requests; it does not control Kubernetes deployment.</p>
                        </div>
                        <div>
                            <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Parallelism</span>
                            <input type="number" min="1" max="32" className={inputClass} value={benchParams.parallelism} onChange={(event) => setBenchParams((previous) => ({ ...previous, parallelism: event.target.value }))} />
                        </div>
                        <div>
                            <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Accelerator DRA profile</span>
                            <input className={inputClass} value={benchParams.accelerator_profile} placeholder="Auto from deployment" onChange={(event) => setBenchParams((previous) => ({ ...previous, accelerator_profile: event.target.value }))} />
                            <p className="mt-1 text-[10px] text-slate-500">For this Intel XPU cluster the resolved runtime profile is intel-xpu.</p>
                        </div>
                        <div className="flex items-end justify-end">
                            <button type="button" disabled={benchBusy || !benchTargetId || !canBenchmarkExisting} title={canBenchmarkExisting ? undefined : "You cannot benchmark deployments"} onClick={runExistingBenchmark} className="inline-flex h-9 items-center gap-2 border border-emerald-500 bg-emerald-500/10 px-3 text-xs text-emerald-200 disabled:opacity-40">
                                <Play className="h-4 w-4" /> Run benchmark
                            </button>
                        </div>
                    </div>
                )}
                {benchNotice && <p className="mt-2 text-xs text-emerald-300">{benchNotice}</p>}
            </section>
            <div className="grid gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(420px,0.85fr)]">
                <form
                    onSubmit={(event) => {
                        event.preventDefault();
                        start();
                    }}
                    className="space-y-4"
                >
                    <section className="border border-slate-800 bg-slate-950/40 p-4">
                        <h2 className="mb-3 text-sm font-semibold">
                            Runtime environment overrides
                        </h2>
                        <p className="text-[10px] text-slate-500">Official source, Guide, image/build settings, and topology come from each immutable Configuration artifact. Evaluate only binds environment-specific proxy values.</p>
                        <div className="mt-3 grid gap-3 md:grid-cols-3">
                            <label className="text-xs text-slate-400">
                                HTTP proxy
                                <input
                                    className={inputClass}
                                    value={runtime.http_proxy}
                                    onChange={(event) =>
                                        setRuntime({
                                            ...runtime,
                                            http_proxy: event.target.value,
                                        })
                                    }
                                />
                            </label>
                            <label className="text-xs text-slate-400">
                                HTTPS proxy
                                <input
                                    className={inputClass}
                                    value={runtime.https_proxy}
                                    onChange={(event) =>
                                        setRuntime({
                                            ...runtime,
                                            https_proxy: event.target.value,
                                        })
                                    }
                                />
                            </label>
                            <label className="text-xs text-slate-400">
                                NO_PROXY
                                <input
                                    className={inputClass}
                                    value={runtime.no_proxy}
                                    onChange={(event) =>
                                        setRuntime({
                                            ...runtime,
                                            no_proxy: event.target.value,
                                        })
                                    }
                                />
                            </label>
                        </div>
                    </section>
                    <section className="border border-slate-800 bg-slate-950/40 p-4">
                        <h2 className="text-sm font-semibold">llm-d-benchmark runtime</h2>
                        <p className="mt-1 text-[10px] text-slate-500">Resolved from the connected cluster's Software Versions record.</p>
                        <div className="mt-3 border border-slate-800 bg-slate-900/30 p-3 text-[11px] text-slate-400">
                            <p><span className="text-slate-500">Local path:</span> {clusterRecord?.llmDBenchmarkRepoPath || "Not downloaded"}</p>
                            <p className="mt-1 break-all"><span className="text-slate-500">Version:</span> {clusterRecord?.llmDBenchmarkRef || "Not configured"}</p>
                        </div>
                    </section>
                    <div className="flex justify-between">
                        <h2 className="text-sm font-semibold">Workloads</h2>
                        <button
                            type="button"
                            onClick={() =>
                                setWorkloads([
                                    ...workloads,
                                    newWorkload(
                                        `workload-${workloads.length + 1}`,
                                    ),
                                ])
                            }
                            className="inline-flex items-center gap-1 border border-slate-700 px-2 py-1 text-xs"
                        >
                            <Plus className="h-3.5 w-3.5" />
                            Add
                        </button>
                    </div>
                    {workloads.map((item, index) => (
                        <WorkloadEditor
                            key={index}
                            value={item}
                            index={index}
                            artifact={configurationArtifact}
                            onChange={(patch) => updateWorkload(index, patch)}
                            onDuplicate={() => setWorkloads((items) => [
                                ...items.slice(0, index + 1),
                                {
                                    ...item,
                                    id: `${item.id}-load-${items.length + 1}`,
                                    benchmark: { ...item.benchmark, parallelism: Math.min(32, Math.max(2, Number(item.benchmark.parallelism || 1) * 2)) },
                                },
                                ...items.slice(index + 1),
                            ])}
                            onRemove={
                                workloads.length > 1
                                    ? () =>
                                          setWorkloads(
                                              workloads.filter(
                                                  (_item, current) =>
                                                      current !== index,
                                              ),
                                          )
                                    : null
                            }
                        />
                    ))}
                    {!canDesignEvaluation && <p className="text-xs text-amber-300">Your role can only benchmark an existing deployment. Designing and deploying a new one requires the deployment-create permission.</p>}
                    <div className="flex items-end justify-between border-t border-slate-800 pt-4">
                        <label className="text-xs text-slate-400">
                            Evaluation name
                            <input
                                className={inputClass}
                                value={name}
                                onChange={(event) =>
                                    setName(event.target.value)
                                }
                            />
                        </label>
                        <button
                            disabled={busy || !session || !canDesignEvaluation || (!configurationArtifact && workloads.some((item) => !item.configuration_artifact_id))}
                            title={canDesignEvaluation ? undefined : "Designing and deploying a new deployment requires the deployment-create permission"}
                            className="inline-flex h-10 items-center gap-2 bg-cyan-500 px-4 text-sm font-semibold text-slate-950 disabled:opacity-40"
                        >
                            <Play className="h-4 w-4" />
                            Start evaluation
                        </button>
                    </div>
                </form>
                <aside className="space-y-3">
                    <div className="flex justify-between">
                        <h2 className="flex items-center gap-2 text-sm font-semibold">
                            <Activity className="h-4 w-4 text-emerald-300" />
                            Runs
                        </h2>
                        <button
                            onClick={() =>
                                loadRuns().catch((nextError) =>
                                    setError(nextError.message),
                                )
                            }
                        >
                            <RefreshCw className="h-4 w-4" />
                        </button>
                    </div>
                    {runs.map((run) => (
                        <div
                            key={run.id}
                            className="border border-slate-800 bg-slate-950/40 p-3"
                        >
                            <button
                                onClick={() =>
                                    onNavigate("optimization-evaluation-details", {
                                        evaluationId: run.id,
                                    })
                                }
                                className="w-full text-left"
                            >
                                <div className="flex gap-2">
                                    <span className="min-w-0 flex-1 truncate font-mono text-xs">
                                        {run.name || run.model || run.id}
                                    </span>
                                    <span
                                        className={`border px-2 py-0.5 text-[10px] ${evaluationStatusTone(run.status)}`}
                                    >
                                        {evaluationStatusLabel(run.status)}
                                    </span>
                                    <ChevronRight className="h-4 w-4" />
                                </div>
                                <p className="mt-2 truncate text-[10px] text-slate-600">
                                    {run.id}
                                </p>
                            </button>
                            <div className="mt-2 flex justify-end gap-1">
                                {["failed", "cancelled"].includes(run.status) &&
                                    run.cases && (
                                        <button
                                            title="Retry"
                                            onClick={() => action(run, "retry")}
                                        >
                                            <RotateCcw className="h-3.5 w-3.5" />
                                        </button>
                                    )}
                                {!TERMINAL_EVALUATION_STATUSES.has(run.status) && (
                                    <button
                                        title="Cancel"
                                        onClick={() => action(run, "cancel")}
                                    >
                                        <Square className="h-3.5 w-3.5" />
                                    </button>
                                )}
                                {TERMINAL_EVALUATION_STATUSES.has(run.status) && (
                                    <button
                                        title="Delete"
                                        onClick={() => action(run, "delete")}
                                    >
                                        <Trash2 className="h-3.5 w-3.5 text-red-300" />
                                    </button>
                                )}
                            </div>
                        </div>
                    ))}
                    {!runs.length && (
                        <p className="flex gap-2 py-8 text-sm text-slate-500">
                            <Clock3 className="h-4 w-4" />
                            No evaluations yet.
                        </p>
                    )}
                    {benchmarkRuns.length > 0 && (
                        <div className="border-t border-slate-800 pt-3">
                            <h3 className="mb-2 text-xs font-semibold text-slate-400">Standalone benchmarks</h3>
                            {benchmarkRuns.map((run) => (
                                <div key={run.id} className="mb-2 border border-slate-800 p-2 text-xs">
                                    <div className="flex items-center justify-between gap-2">
                                        <span className="truncate font-mono">{run.id}</span>
                                        <span className={`border px-2 py-0.5 text-[10px] ${evaluationStatusTone(run.status)}`}>{evaluationStatusLabel(run.status)}</span>
                                    </div>
                                    <p className="mt-1 text-[10px] text-slate-500">{run.workload} · parallelism {run.parallelism}</p>
                                </div>
                            ))}
                        </div>
                    )}
                </aside>
            </div>
        </section>
    );
}
