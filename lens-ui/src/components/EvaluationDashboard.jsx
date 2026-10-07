import { PaginationControls } from './ui/PaginationControls';
import { resolveEvaluationTarget } from '../features/evaluation/client';
import { editedEvaluationConfiguration } from '../features/evaluation/configuration';
import { evaluationApi as api } from '../features/evaluation/client';
import { TERMINAL_EVALUATION_STATUSES, ACTIVE_EVALUATION_STATUSES } from '../features/evaluation/domain';
import { confirmDelete } from "./ui/confirmDelete";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
    Activity,
    AlertTriangle,
    BarChart3,
    CheckCircle2,
    ChevronLeft,
    ChevronRight,
    CircleDashed,
    Code2,
    FileCode2,
    Gauge,
    Info,
    Play,
    Plus,
    RefreshCw,
    Search,
    Server,
    Trash2,
    Upload,
    Workflow,
} from "lucide-react";
import { deleteConfigurationArtifact, loadConfigurationArtifacts, renderConfiguration, saveConfiguration } from "./OptimizationWorkspace/configurationBackend";
import { loadGuideCatalog, planGuideDeployment } from "./OptimizationWorkspace/guidePlanningBackend";
import { listDeploymentExecutions } from "./OptimizationWorkspace/remoteDeployBackend";
import { listModelNames } from "./ModelService/modelServiceBackend";
import { MultiSelectDropdown } from "./common/MultiSelectDropdown";
import { Modal } from "./ui/Modal";
import { ModuleHeader } from "./ui/ModuleHeader";
import { ModulePage } from "./ui/ModulePage";
import { useAuth } from "../features/auth/useAuth";
import { usePolling } from "../hooks/usePolling";
import { returnedEvaluationArtifactIds } from "../utils/evaluationArtifactSelection";
import TaskCard from "./evaluation/EvaluationTaskCard";
import { evaluationTasks } from "./evaluation/evaluationTasks";
import { createDashboardResource } from "./evaluation/dashboardResource";

const taskResource = createDashboardResource(async () => {
    const [workflows, benchmarks] = await Promise.all([
        api("/api/v1/evaluate/workflow-runs"),
        api("/api/v1/evaluate/runs"),
    ]);
    return { runs: workflows.items || [], benchmarkRuns: benchmarks.items || [] };
});
const deploymentResource = createDashboardResource(() => listDeploymentExecutions({ status: "ready" }));

const INTENT_KEY = "prism_evaluate_pending_intent";
const PREFILL_KEY = "prism_evaluate_prefill_workloads";
const ARTIFACT_KEY = "prism_evaluate_configuration_artifact";
const inputClass = "mt-1 h-10 w-full rounded-lg border border-slate-700 bg-slate-950/80 px-3 text-xs text-slate-100 outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/10";
// Conservative defaults keep ad-hoc development evaluations short. Users can expand
// the prompt set, token lengths, and rate stages for an intentional saturation run.
const DEFAULT_SHARED_PREFIX_CONFIG = {
    num_groups: 12,
    num_prompts_per_group: 2,
    system_prompt_len: 1024,
    question_len: 128,
    output_len: 128,
    enable_multi_turn_chat: false,
};
const defaultRuntime = {
    http_proxy: "http://proxy.ims.intel.com:911",
    https_proxy: "http://proxy.ims.intel.com:911",
    no_proxy: "intel.com,.intel.com,localhost,127.0.0.1",
};
const DEFAULT_WORKLOAD_YAML = `load:
    type: constant
    stages:
        - rate: 1
          duration: 30
    request_timeout: 30
    num_workers: 1
api:
    type: completion
    streaming: true
data:
    type: random
    input_distribution:
        min: 256
        max: 512
        mean: 384
        total_count: 20
    output_distribution:
        min: 10
        max: 100
        mean: 50
        total_count: 20
report:
    request_lifecycle:
        summary: true
        per_stage: true
        per_request: true`;

    const BUILT_IN_SUITES = [
        {
            id: "quick",
            name: "Quick validation",
            description: "Fast smoke test before longer measurements.",
            scenarios: [
                { id: "smoke", name: "Basic functionality", description: "Short single-user request flow.", isl: 128, osl: 64, concurrency: 1, requests: 10, ttft_ms: 500, tpot_ms: 50 },
            ],
        },
        {
            id: "interactive",
            name: "Interactive chat",
            description: "Single-user, multi-user, and burst-like chat loads.",
            scenarios: [
                { id: "single_user", name: "Single user", isl: 256, osl: 256, concurrency: 1, requests: 100, ttft_ms: 500, tpot_ms: 50 },
                { id: "multi_user", name: "Multi user", isl: 256, osl: 256, concurrency: 8, requests: 200, ttft_ms: 500, tpot_ms: 50 },
                { id: "traffic_spike", name: "Traffic spike", isl: 128, osl: 128, concurrency: 32, requests: 100, ttft_ms: 750, tpot_ms: 75 },
            ],
        },
        {
            id: "long_context",
            name: "Long context",
            description: "Measures medium through very-long prompt handling.",
            scenarios: [
                { id: "medium_context", name: "4K context", isl: 4096, osl: 512, concurrency: 2, requests: 30, ttft_ms: 5000, tpot_ms: 100 },
                { id: "long_context", name: "8K context", isl: 8192, osl: 512, concurrency: 1, requests: 20, ttft_ms: 5000, tpot_ms: 100 },
                { id: "very_long_context", name: "16K context", isl: 16384, osl: 256, concurrency: 1, requests: 10, ttft_ms: 8000, tpot_ms: 120 },
            ],
        },
        {
            id: "throughput",
            name: "Throughput sweep",
            description: "One exact token shape across increasing closed-loop concurrency.",
            scenarios: [
                { id: "throughput_sweep", name: "Concurrency sweep", isl: 128, osl: 64, concurrencyStages: [{ concurrency: 1, num_requests: 32 }, { concurrency: 16, num_requests: 96 }, { concurrency: 64, num_requests: 192 }, { concurrency: 128, num_requests: 256 }], throughput_min_tps: 0 },
            ],
        },
    ];

    function suiteScenarioPayload(scenario, defaults) {
        const stages = scenario.concurrencyStages || [{
            concurrency: Number(scenario.concurrency || defaults.concurrency),
            num_requests: Number(scenario.requests || defaults.requests),
        }];
        return {
            id: scenario.id,
            name: scenario.name,
            description: scenario.description || "",
            benchmark: {
                harness: "inference-perf",
                workload: `${scenario.id}.yaml`,
                parallelism: Number(defaults.parallelism),
                wait_timeout_seconds: Number(defaults.wait_timeout_seconds),
                matrix: [{ isl: Number(scenario.isl || defaults.isl), osl: Number(scenario.osl || defaults.osl) }],
                concurrency_stages: stages.map((stage) => ({ concurrency: Number(stage.concurrency), num_requests: Number(stage.num_requests) })),
                warmup_requests: Number(defaults.warmup_requests),
            },
            sla_targets: {
                ttft_ms: scenario.ttft_ms ? Number(scenario.ttft_ms) : null,
                ttft_percentile: "p99",
                tpot_ms: scenario.tpot_ms ? Number(scenario.tpot_ms) : null,
                tpot_percentile: "p99",
                throughput_min_tps: scenario.throughput_min_tps != null ? Number(scenario.throughput_min_tps) : null,
                success_rate_min_percent: 99,
            },
        };
    }

function normalizeWorkloadYaml(value) {
    return String(value || DEFAULT_WORKLOAD_YAML).replace(
        /^(\s*)- rate:([^\n]*)\n\s+duration:/gm,
        (_, indent, rate) => `${indent}- rate:${rate}\n${indent}  duration:`,
    );
}



function artifactDetails(artifact) {
    const configuration = artifact?.deployable_configuration || {};
    const content = configuration.content || {};
    const decode = content.decode || content.serving || {};
    const guide = configuration.provider_ref || configuration.type || "Configuration";
    const rawVariant = content.guideVariant || content.officialGuide?.source?.variant || "";
    const variant = rawVariant === "." ? "" : rawVariant;
    return {
        model: content.model?.name || configuration.type || "Unknown model",
        guide,
        variant,
        name: artifact?.configuration_file?.file_name || `${guide}${variant ? ` · ${variant}` : ""}`,
        topology: `${decode.replicaCount ?? "—"} replica · TP ${decode.tensorParallelSize ?? "—"}`,
        cluster: configuration.provenance?.cluster_ref?.name || configuration.provenance?.cluster_ref?.id || "Unbound cluster",
    };
}

function integerVariants(value, fallback, label) {
    const values = String(value || fallback).split(",").map((item) => Number(item.trim())).filter(Number.isFinite);
    const unique = [...new Set(values)];
    if (!unique.length || unique.some((item) => !Number.isInteger(item) || item < 1)) {
        throw new Error(`${label} must contain comma-separated positive integers.`);
    }
    return unique;
}

function ComparisonPlan({ guides, selectedGuides, selectedArtifacts, parameters, editingGuide, includeRawVllm, needsKubernetesServiceComparison, onToggleGuide, onEditGuide, onUpdateParameter, onToggleRawVllm }) {
    const source = selectedArtifacts[0]?.deployable_configuration?.content || {};
    const decode = source.decode || source.serving || {};
    const prefill = source.prefill || {};
    const field = (guide, key, fallback, label, options = {}) => <label><span className="text-[9px] font-medium uppercase tracking-wide text-slate-500">{label}</span><input {...options} className={inputClass} value={parameters[guide]?.[key] ?? fallback} onChange={(event) => onUpdateParameter(guide, key, event.target.value)} /></label>;
    return <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/45 p-3">
        <div><p className="text-xs font-semibold text-slate-300">Comparison plan</p><p className="mt-0.5 text-[9px] text-slate-600">Add real Guide deployments to this Evaluation. All arms use the selected model, runtime, and benchmark workload.</p></div>
        {selectedArtifacts.length !== 1 && <p className="mt-2 rounded border border-amber-500/25 bg-amber-500/5 p-2 text-[9px] text-amber-200">Select exactly one primary configuration to add official comparison Guides.</p>}
        <div className="mt-3 grid gap-2 md:grid-cols-2">
            {guides.map((guide) => {
                const selected = selectedGuides.includes(guide.id);
                const editing = editingGuide === guide.id;
                const isPd = guide.id === "pd-disaggregation";
                return <div key={guide.id} className={`rounded border p-2.5 ${selected ? "border-cyan-500/40 bg-cyan-500/10" : "border-slate-800"}`}>
                    <div className="flex items-start gap-2"><input type="checkbox" disabled={selectedArtifacts.length !== 1} checked={selected} onChange={() => onToggleGuide(guide.id)} className="mt-0.5 accent-cyan-400" /><button type="button" disabled={selectedArtifacts.length !== 1} onClick={() => onToggleGuide(guide.id)} className="min-w-0 flex-1 text-left disabled:opacity-50"><span className="block text-[11px] font-semibold text-slate-200">{guide.label || guide.id}</span><span className="mt-0.5 block text-[9px] text-slate-500">Official Guide deployment; customize topology and key vLLM limits.</span></button><button type="button" disabled={selectedArtifacts.length !== 1} onClick={() => onEditGuide(editing ? "" : guide.id)} className="shrink-0 border border-slate-700 px-2 py-1 text-[9px] text-cyan-300 disabled:opacity-40">{editing ? "Done" : "Edit parameters"}</button></div>
                    {editing && <div className="mt-3 grid gap-2 border-t border-slate-800 pt-3 sm:grid-cols-2">
                        {isPd && <>{field(guide.id, "prefillReplicas", prefill.replicaCount || 1, "Prefill replicas")}{field(guide.id, "prefillTensorParallelSize", prefill.tensorParallelSize || decode.tensorParallelSize || 1, "Prefill TP")}</>}
                        {field(guide.id, "replicas", decode.replicaCount || 1, isPd ? "Decode replicas" : "Replicas")}
                        {field(guide.id, "tensorParallelSize", decode.tensorParallelSize || 1, isPd ? "Decode TP" : "Tensor parallel size")}
                        {field(guide.id, "maxModelLen", decode.maxModelLen || decode.max_model_len || 16384, "Maximum model length", { type: "number", min: 1 })}
                        {field(guide.id, "maxNumSeqs", decode.maxNumSeqs || decode.max_num_seqs || 64, "Maximum concurrent sequences", { type: "number", min: 1 })}
                        <div className="sm:col-span-2">{field(guide.id, "gpuMemoryUtilization", 0.9, "GPU/XPU memory utilization", { type: "number", min: 0.1, max: 1, step: 0.01 })}</div>
                        <p className="sm:col-span-2 text-[9px] text-slate-500">Replica and TP fields accept comma-separated variants. For example, optimized-baseline replicas “2,4” creates Aggregate-2 and Aggregate-4.</p>
                    </div>}
                </div>;
            })}
            <label className={`flex cursor-pointer items-start gap-2 rounded border p-2.5 ${includeRawVllm ? "border-amber-500/40 bg-amber-500/10" : "border-slate-800"}`}><input type="checkbox" checked={includeRawVllm} onChange={onToggleRawVllm} className="mt-0.5 accent-amber-400" /><span><span className="block text-[11px] font-semibold text-slate-200">Raw vLLM</span><span className="mt-0.5 block text-[9px] text-slate-500">Deploys a real plain vLLM reference without llm-d Router for every selected arm.</span></span></label>
        </div>
        {needsKubernetesServiceComparison && <div className="mt-3 rounded border border-emerald-500/30 bg-emerald-500/10 p-2.5 text-[9px] text-emerald-200">The required same-pod Kubernetes Service round-robin comparison is included automatically.</div>}
        {!selectedGuides.length && !includeRawVllm && !needsKubernetesServiceComparison && <p className="mt-2 text-[9px] text-slate-500">No comparison selected — absolute candidate metrics only.</p>}
    </div>;
}

export default function EvaluationDashboard({ onNavigate }) {
    const { can } = useAuth();
    // Users who can only benchmark an existing deployment may still open the
    // wizard; it locks itself to the "Use existing endpoint" target.
    const canCreateEvaluation = can('evaluate:workflow:create') || can('evaluate:run:create');
    const [runs, setRuns] = useState(() => taskResource.getSnapshot()?.runs || []);
    const [benchmarkRuns, setBenchmarkRuns] = useState(() => taskResource.getSnapshot()?.benchmarkRuns || []);
    const [readyDeployments, setReadyDeployments] = useState(() => deploymentResource.getSnapshot() || []);
    const [artifacts, setArtifacts] = useState([]);
    const [selectedArtifacts, setSelectedArtifacts] = useState([]);
    const [loading, setLoading] = useState(() => !taskResource.getSnapshot());
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [isNewTaskOpen, setIsNewTaskOpen] = useState(false);
    const [wizardStep, setWizardStep] = useState(0);
    const wizardTopRef = useRef(null);
    const wizardNextRef = useRef(null);
    const [mode, setMode] = useState("deploy");
    const [name, setName] = useState("Evaluation");
    const [benchmark, setBenchmark] = useState({ harness: "inference-perf", workload: "sanity_random.yaml", parallelism: 1, wait_timeout_seconds: 1800 });
    const [suiteProfileId, setSuiteProfileId] = useState("custom");
    const [suiteScenarioIds, setSuiteScenarioIds] = useState([]);
    const [suiteDefaults, setSuiteDefaults] = useState({ isl: 256, osl: 128, concurrency: 1, requests: 16 });
    const [comparisonCatalog, setComparisonCatalog] = useState(null);
    const [comparisonGuides, setComparisonGuides] = useState([]);
    const [comparisonParameters, setComparisonParameters] = useState({});
    const [editingComparisonGuide, setEditingComparisonGuide] = useState("");
    const [includeRawVllm, setIncludeRawVllm] = useState(false);
    const [targetId, setTargetId] = useState("");
    const [modelServices, setModelServices] = useState([]);
    const [apiToken, setApiToken] = useState("");
    const [configurationTab, setConfigurationTab] = useState("select");
    const [inspectedArtifactId, setInspectedArtifactId] = useState("");
    const [preserveDeployment, setPreserveDeployment] = useState(false);
    const [yamlText, setYamlText] = useState("");
    const [yamlLoading, setYamlLoading] = useState(false);
    const [yamlSaving, setYamlSaving] = useState(false);
    const [workloadExpanded, setWorkloadExpanded] = useState(false);
    const [workloadYaml, setWorkloadYaml] = useState(() => normalizeWorkloadYaml(DEFAULT_WORKLOAD_YAML));
    const [matrixExpanded, setMatrixExpanded] = useState(false);
    const [matrixPoints, setMatrixPoints] = useState([]);
    const [concurrencyStages, setConcurrencyStages] = useState([]);
    const [warmupRequests, setWarmupRequests] = useState(2);
    const [sharedPrefixExpanded, setSharedPrefixExpanded] = useState(false);
    const [sharedPrefixConfig, setSharedPrefixConfig] = useState(DEFAULT_SHARED_PREFIX_CONFIG);
    const [rateStages, setRateStages] = useState([]);
    const [statusFilter, setStatusFilter] = useState("");
    const [searchInput, setSearchInput] = useState("");
    const [modeFilters, setModeFilters] = useState(new Set());
    const [workloadFilters, setWorkloadFilters] = useState(new Set());
    const [taskPage, setTaskPage] = useState(1);
    const [taskPageSize, setTaskPageSize] = useState(10);
    const selectedArtifactObjects = artifacts.filter((artifact) => selectedArtifacts.includes(artifact.artifact_id));
    const selectedClusterSessions = [...new Set(selectedArtifactObjects.map((artifact) => artifact.deployable_configuration?.provenance?.cluster_ref?.session_id).filter(Boolean))];
    const selectedClusterNames = [...new Set(selectedArtifactObjects.map((artifact) => artifactDetails(artifact).cluster).filter(Boolean))];
    const selectedGuides = [...new Set(selectedArtifactObjects.map((artifact) => artifact.deployable_configuration?.provider_ref).filter(Boolean))];
    const needsKubernetesServiceComparison = selectedGuides.length === 1
        && selectedGuides[0] === 'precise-prefix-cache-routing';
    const comparisonGuideOptions = (comparisonCatalog?.guides || []).filter((guide) => (
        guide.deploymentCapability && !selectedGuides.includes(guide.id)
    ));
    const successfulArtifactIds = useMemo(() => new Set(runs.flatMap((run) => (run.cases || [])
        .filter((item) => item.kind === "guide" && item.status === "succeeded" && item.configuration_artifact_id)
        .map((item) => item.configuration_artifact_id))), [runs]);
    const visibleArtifacts = useMemo(() => artifacts.filter((artifact) => (
        selectedArtifacts.includes(artifact.artifact_id) || successfulArtifactIds.has(artifact.artifact_id)
    )), [artifacts, selectedArtifacts, successfulArtifactIds]);

    const loadAll = async ({ quiet = false } = {}) => {
        if (!quiet) setLoading(true);
        try {
            const snapshot = await taskResource.refresh();
            setRuns(snapshot.runs);
            setBenchmarkRuns(snapshot.benchmarkRuns);
            setError("");
        } catch (nextError) {
            setError(nextError.message);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        if (!isNewTaskOpen) return;
        let active = true;
        loadGuideCatalog().then(value => { if (active) setComparisonCatalog(value); }).catch(nextError => { if (active) setError(nextError.message); });
        loadConfigurationArtifacts().then(value => { if (active) setArtifacts(value); }).catch(nextError => { if (active) setError(nextError.message); });
        listModelNames().then(items => {
            if (!active) return;
            setModelServices(items);
            setTargetId(current => items.some(item => item.id === current) ? current : items[0]?.id || "");
        }).catch(nextError => { if (active) setError(nextError.message); });
        return () => { active = false; };
    }, [isNewTaskOpen]);

    const refreshReadyDeployments = useCallback(() => deploymentResource.refresh().then(items => {
        setReadyDeployments(items);
    }).catch(() => {}), []);
    useEffect(() => {
        if (isNewTaskOpen) return;
        refreshReadyDeployments();
    }, [isNewTaskOpen, refreshReadyDeployments]);
    usePolling(refreshReadyDeployments, { enabled: !isNewTaskOpen });

    // Configuration publishes immutable artifacts, then returns here so the user can review
    // and edit Benchmark traffic before explicitly launching. Creation must never deploy by
    // itself: deployment is owned only by the New Task "Launch evaluation" action below.
    useEffect(() => {
        const readOnce = (key) => {
            const raw = sessionStorage.getItem(key);
            sessionStorage.removeItem(key);
            if (!raw) return null;
            try { return JSON.parse(raw); } catch { return null; }
        };
        readOnce(INTENT_KEY);
        const plans = readOnce(PREFILL_KEY);
        const artifact = readOnce(ARTIFACT_KEY);
        if (!Array.isArray(plans) || !plans.length || !artifact?.artifact_id) {
            loadAll();
            return;
        }
        // A publish from Configuration replaces the prior wizard selection. Merging the
        // old intent here silently adds stale configurations to the new evaluation.
        const artifactIds = returnedEvaluationArtifactIds(plans, artifact.artifact_id);
        setArtifacts((current) => [artifact, ...current.filter((item) => item.artifact_id !== artifact.artifact_id)]);
        setSelectedArtifacts(artifactIds);
        setMode("deploy");
        setName(`Evaluate ${artifactDetails(artifact).guide}`);
        const generatedBenchmark = plans[0]?.benchmark;
        if (generatedBenchmark) {
            setBenchmark((current) => ({ ...current, ...generatedBenchmark }));
            setWorkloadYaml(normalizeWorkloadYaml(generatedBenchmark.workload_yaml || DEFAULT_WORKLOAD_YAML));
            setMatrixPoints(generatedBenchmark.matrix || []);
            setConcurrencyStages(generatedBenchmark.concurrency_stages || []);
            setWarmupRequests(generatedBenchmark.warmup_requests ?? 2);
            setRateStages(generatedBenchmark.shared_prefix?.stages || []);
            if (generatedBenchmark.shared_prefix) setSharedPrefixConfig(generatedBenchmark.shared_prefix);
        }
        setIncludeRawVllm(false);
        setComparisonGuides([]);
        setComparisonParameters({});
        setPreserveDeployment(plans.some((plan) => plan.preserve_deployment));
        setWizardStep(0);
        setIsNewTaskOpen(true);
        window.requestAnimationFrame(() => window.requestAnimationFrame(() => wizardNextRef.current?.scrollIntoView({ block: "end" })));
        loadAll({ quiet: true });
    }, []);
    // Always poll -- not just while a run is active -- so tasks created
    // or changed from another tab/session (or backend automation) still
    // show up here without a manual refresh.
    usePolling(() => loadAll({ quiet: true }));

    const tasks = useMemo(() => {
        return evaluationTasks(runs, benchmarkRuns, readyDeployments);
    }, [runs, benchmarkRuns, readyDeployments]);
    const summary = useMemo(() => ({
        total: tasks.length,
        active: tasks.filter((task) => ACTIVE_EVALUATION_STATUSES.has(task.status)).length,
        succeeded: tasks.filter((task) => task.status === "succeeded").length,
        comparisons: runs.filter((task) => Object.keys(task.configuration_artifacts || {}).length > 1).length,
    }), [tasks, runs]);
    const statusCounts = useMemo(() => ({
        succeeded: tasks.filter((task) => task.status === "succeeded").length,
        running: tasks.filter((task) => ACTIVE_EVALUATION_STATUSES.has(task.status) && task.status !== "queued").length,
        queued: tasks.filter((task) => task.status === "queued").length,
        failed: tasks.filter((task) => task.status === "failed").length,
        cancelled: tasks.filter((task) => task.status === "cancelled").length,
    }), [tasks]);
    const taskWorkload = (task) => task.workload || task.cases?.[0]?.benchmark?.workload || "Unknown";
    const filterOptions = useMemo(() => ({
        modes: ["Deploy + benchmark", "Existing endpoint"],
        workloads: [...new Set(tasks.map(taskWorkload))].sort(),
    }), [tasks]);
    const filteredTasks = useMemo(() => {
        const query = searchInput.trim().toLowerCase();
        return tasks.filter((task) => {
            const normalizedStatus = ACTIVE_EVALUATION_STATUSES.has(task.status) ? "running" : task.status;
            const taskMode = task.kind === "workflow" ? "Deploy + benchmark" : "Existing endpoint";
            if (statusFilter && normalizedStatus !== statusFilter) return false;
            if (modeFilters.size && !modeFilters.has(taskMode)) return false;
            if (workloadFilters.size && !workloadFilters.has(taskWorkload(task))) return false;
            if (query && ![task.name, task.model, task.id, taskMode, taskWorkload(task), task.harness].some((value) => String(value || "").toLowerCase().includes(query))) return false;
            return true;
        });
    }, [tasks, statusFilter, modeFilters, workloadFilters, searchInput]);
    const taskPageCount = Math.max(1, Math.ceil(filteredTasks.length / taskPageSize));
    const pagedTasks = useMemo(() => filteredTasks.slice((taskPage - 1) * taskPageSize, taskPage * taskPageSize), [filteredTasks, taskPage, taskPageSize]);
    useEffect(() => { setTaskPage(1); }, [statusFilter, modeFilters, workloadFilters, searchInput, taskPageSize]);
    useEffect(() => { if (taskPage > taskPageCount) setTaskPage(taskPageCount); }, [taskPage, taskPageCount]);
    const toggleFilter = (setter, value) => setter((current) => {
        if (!value) return new Set();
        const next = new Set(current);
        if (next.has(value)) next.delete(value); else next.add(value);
        return next;
    });
    const clearFilters = () => { setStatusFilter(""); setSearchInput(""); setModeFilters(new Set()); setWorkloadFilters(new Set()); };

    const toggleArtifact = (artifactId) => setSelectedArtifacts((current) => current.includes(artifactId) ? current.filter((id) => id !== artifactId) : [...current, artifactId]);
    const deleteArtifact = async (artifactId) => {
        if (!await confirmDelete("Delete this saved deployment YAML? Historical Evaluation evidence is retained.")) return;
        try {
            await deleteConfigurationArtifact(artifactId);
            setArtifacts((current) => current.filter((item) => item.artifact_id !== artifactId));
            setSelectedArtifacts((current) => current.filter((id) => id !== artifactId));
            if (inspectedArtifactId === artifactId) {
                setInspectedArtifactId("");
                setConfigurationTab("select");
                setYamlText("");
            }
        } catch (nextError) {
            setError(nextError.message);
        }
    };
    const toggleComparisonGuide = (guide) => setComparisonGuides((current) => current.includes(guide)
        ? current.filter((item) => item !== guide)
        : [...current, guide]);
    const updateComparisonParameter = (guide, key, value) => setComparisonParameters((current) => ({
        ...current,
        [guide]: { ...(current[guide] || {}), [key]: value },
    }));
    const goToWizardStep = (step) => {
        setWizardStep(step);
        window.requestAnimationFrame(() => wizardTopRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    };

    const openNewTask = () => {
        setWizardStep(0);
        setWorkloadYaml(normalizeWorkloadYaml(DEFAULT_WORKLOAD_YAML));
        setIsNewTaskOpen(true);
    };

    const selectSuiteProfile = (profileId) => {
        setSuiteProfileId(profileId);
        const profile = BUILT_IN_SUITES.find((item) => item.id === profileId);
        setSuiteScenarioIds(profile ? profile.scenarios.map((scenario) => scenario.id) : []);
    };

    const inspectArtifact = async (artifactId) => {
        if (!artifactId) return;
        setInspectedArtifactId(artifactId);
        setConfigurationTab("yaml");
        setYamlLoading(true);
        try {
            const response = await fetch(`/api/v1/configurations/artifacts/${artifactId}/manifest`);
            if (!response.ok) throw new Error(`Unable to load YAML (${response.status})`);
            setYamlText(await response.text());
        } catch (nextError) {
            setError(nextError.message);
        } finally {
            setYamlLoading(false);
        }
    };

    const saveYamlVariant = async () => {
        const source = artifacts.find((artifact) => artifact.artifact_id === inspectedArtifactId);
        if (!source || !yamlText.trim()) return;
        setYamlSaving(true);
        try {
            const nextConfiguration = await editedEvaluationConfiguration(source, yamlText);
            const saved = await saveConfiguration(nextConfiguration, `evaluation-edited-${Date.now()}`);
            const nextArtifact = saved.artifact;
            setArtifacts((current) => [nextArtifact, ...current]);
            setSelectedArtifacts((current) => [...current.filter((id) => id !== source.artifact_id), nextArtifact.artifact_id]);
            setInspectedArtifactId(nextArtifact.artifact_id);
            setError("");
        } catch (nextError) {
            setError(nextError.message);
        } finally {
            setYamlSaving(false);
        }
    };

    const goToConfiguration = () => {
        sessionStorage.setItem(INTENT_KEY, JSON.stringify({
            operation: "create-evaluation",
            return_target: "optimization-evaluate",
            name,
            runtime: defaultRuntime,
            workloads: [],
            selected_artifact_ids: selectedArtifacts,
            preserve_deployment: preserveDeployment,
            created_at: new Date().toISOString(),
        }));
        setIsNewTaskOpen(false);
        onNavigate("optimization-explore");
    };

    const createComparisonArtifacts = async () => {
        if (!comparisonGuides.length) return [];
        if (selectedArtifactObjects.length !== 1) throw new Error("Select exactly one primary configuration before adding comparison Guides.");
        const sourceArtifact = selectedArtifactObjects[0];
        const sourceConfiguration = sourceArtifact.deployable_configuration || {};
        const sourceContent = sourceConfiguration.content || {};
        const sourceDecode = sourceContent.decode || sourceContent.serving || {};
        const sourcePrefill = sourceContent.prefill || {};
        const clusterRef = sourceConfiguration.provenance?.cluster_ref || {};
        const model = sourceContent.model?.name;
        if (!model || !clusterRef.session_id) throw new Error("The primary configuration must include a model and bound cluster session.");
        const savedArtifacts = [];
        for (const guideId of comparisonGuides) {
            const guide = (comparisonCatalog?.guides || []).find((item) => item.id === guideId && item.deploymentCapability);
            const accelerator = guide?.accelerators?.find((item) => item.id === "xpu") || guide?.accelerators?.[0];
            const modelServer = accelerator?.modelServers?.find((item) => item.id === "vllm") || accelerator?.modelServers?.[0];
            if (!guide || !accelerator || !modelServer) throw new Error(`No deployable target is available for ${guideId}.`);
            const parameters = comparisonParameters[guideId] || {};
            const isPd = guideId === "pd-disaggregation";
            const replicas = integerVariants(parameters.replicas, sourceDecode.replicaCount || 1, `${guideId} replicas`);
            const tps = integerVariants(parameters.tensorParallelSize, sourceDecode.tensorParallelSize || 1, `${guideId} TP`);
            const prefillReplicas = isPd ? integerVariants(parameters.prefillReplicas, sourcePrefill.replicaCount || 1, `${guideId} prefill replicas`) : [0];
            const prefillTps = isPd ? integerVariants(parameters.prefillTensorParallelSize, sourcePrefill.tensorParallelSize || sourceDecode.tensorParallelSize || 1, `${guideId} prefill TP`) : [0];
            const variants = prefillReplicas.flatMap((prefillReplicaCount) => prefillTps.flatMap((prefillTpCount) => replicas.flatMap((replicaCount) => tps.map((tpCount) => ({ prefillReplicaCount, prefillTpCount, replicaCount, tpCount })))));
            if (variants.length > 16) throw new Error(`${guideId} comparison is limited to 16 topology combinations.`);
            const guideVariant = guideId === "tiered-prefix-cache"
                ? guide.deploymentCapability.variants?.includes("native/cpu/base") ? "native/cpu/base" : guide.deploymentCapability.variants?.[0] || ""
                : isPd ? "vllm" : "";
            for (const [index, variant] of variants.entries()) {
                const maxModelLen = Number(parameters.maxModelLen || sourceDecode.maxModelLen || sourceDecode.max_model_len || 16384);
                const maxNumSeqs = Number(parameters.maxNumSeqs || sourceDecode.maxNumSeqs || sourceDecode.max_num_seqs || 64);
                const gpuMemoryUtilization = Number(parameters.gpuMemoryUtilization || 0.9);
                const planningRequest = {
                    guide: guideId, accelerator: accelerator.id, modelServer: modelServer.id,
                    source: { mode: "official" }, model,
                    modelSource: sourceContent.runtime?.modelSource || "auto-cache",
                    modelPath: sourceContent.runtime?.mountPath || "/mnt/data/huggingface-cache",
                    replicas: variant.replicaCount, tensorParallelSize: variant.tpCount,
                    prefillReplicas: variant.prefillReplicaCount, prefillTensorParallelSize: variant.prefillTpCount,
                    decodeReplicas: variant.replicaCount, decodeTensorParallelSize: variant.tpCount,
                    maxModelLen, maxNumSeqs, gpuMemoryUtilization,
                    runtimeImage: sourceContent.runtime?.image || "",
                    guideVariant, clusterSessionId: clusterRef.session_id,
                    environment: { mode: "current", kubernetesMode: "required" },
                };
                const plan = await planGuideDeployment(planningRequest);
                if (plan.validation?.status === "invalid" || plan.validation?.errors?.length) throw new Error(`${guideId} R${variant.replicaCount}/TP${variant.tpCount}: ${(plan.validation.errors || []).join("; ")}`);
                const candidateConfig = {
                    schema_version: "1.0", type: isPd ? "pd" : "baseline", candidate_source: { name: "evaluation-comparison" },
                    target: { model, cluster_id: clusterRef.id, cluster_name: clusterRef.name },
                    serving: { replicas: variant.replicaCount, tensor_parallel_size: variant.tpCount, max_model_len: maxModelLen },
                    prefill: isPd ? { replicas: variant.prefillReplicaCount, tensor_parallel_size: variant.prefillTpCount } : null,
                    decode: { replicas: variant.replicaCount, tensor_parallel_size: variant.tpCount, max_model_len: maxModelLen, max_num_seqs: maxNumSeqs },
                    custom_parameters: [
                        { target: "decode", kind: "argument", name: "max-num-seqs", value: String(maxNumSeqs) },
                        { target: "decode", kind: "argument", name: "gpu-memory-utilization", value: String(gpuMemoryUtilization) },
                    ],
                    guide_variant: guideVariant || undefined,
                    runtime: sourceContent.runtime || {},
                };
                const rendered = await renderConfiguration(candidateConfig, {
                    guide_ref: guideId, template_ref: plan.source?.files?.[0], guide_source: plan.source,
                    rendered_manifest: plan.plannedDeployment?.content, cluster_ref: clusterRef,
                    deployment: plan.deployment, modelSecret: sourceContent.officialGuide?.modelSecret || { mode: "none" },
                });
                const saved = await saveConfiguration(rendered.deployable_configuration, `${guideId}-comparison-r${variant.replicaCount}-tp${variant.tpCount}-${Date.now()}-${index}.yaml`);
                savedArtifacts.push(saved.artifact);
            }
        }
        return savedArtifacts;
    };

    const createTask = async () => {
        setBusy(true);
        setError("");
        const normalizedWorkloadYaml = normalizeWorkloadYaml(workloadYaml);
        if (normalizedWorkloadYaml !== workloadYaml) setWorkloadYaml(normalizedWorkloadYaml);
        const sharedPrefixPayload = benchmark.harness === "inference-perf" && rateStages.length ? {
            num_groups: Number(sharedPrefixConfig.num_groups),
            num_prompts_per_group: Number(sharedPrefixConfig.num_prompts_per_group),
            system_prompt_len: Number(sharedPrefixConfig.system_prompt_len),
            question_len: Number(sharedPrefixConfig.question_len),
            output_len: Number(sharedPrefixConfig.output_len),
            enable_multi_turn_chat: Boolean(sharedPrefixConfig.enable_multi_turn_chat),
            stages: rateStages.map((stage) => ({ rate: Number(stage.rate), duration: Number(stage.duration) })),
        } : undefined;
        try {
            if (suiteProfileId !== "custom" && !suiteScenarioIds.length) {
                throw new Error("Select at least one benchmark suite scenario.");
            }
            if (mode === "deploy") {
                if (!selectedArtifacts.length) throw new Error("Select at least one deployment configuration.");
                if (selectedClusterSessions.length !== 1) throw new Error(selectedClusterSessions.length ? "Selected configurations belong to different clusters. Select configurations bound to one cluster." : "The selected configuration has no bound cluster session. Create a new configuration and select its deployment cluster there.");
                const comparisonArtifacts = await createComparisonArtifacts();
                const evaluationArtifactIds = [...selectedArtifacts, ...comparisonArtifacts.map((artifact) => artifact.artifact_id)];
                const evaluationArtifacts = [...selectedArtifactObjects, ...comparisonArtifacts];
                if (comparisonArtifacts.length) {
                    setArtifacts((current) => [...comparisonArtifacts, ...current]);
                    setSelectedArtifacts(evaluationArtifactIds);
                }
                await api("/api/v1/evaluate/evaluations", {
                    method: "POST",
                    body: JSON.stringify({
                        name,
                        cluster_session_id: selectedClusterSessions[0],
                        runtime: defaultRuntime,
                        compare_configurations: true,
                        benchmark_plans: evaluationArtifactIds.map((artifactId, index) => {
                            const requiresKubernetesService = evaluationArtifacts[index]?.deployable_configuration?.provider_ref === "precise-prefix-cache-routing";
                            const baselineTypes = [
                                ...(requiresKubernetesService ? ["kubernetes-service"] : []),
                                ...(includeRawVllm ? ["direct-vllm"] : []),
                            ];
                            return ({
                            id: `configuration-${index + 1}`,
                            configuration_artifact_id: artifactId,
                            benchmark: {
                                ...benchmark,
                                parallelism: Number(benchmark.parallelism),
                                matrix: matrixPoints.map((point) => ({ isl: Number(point.isl), osl: Number(point.osl) })),
                                concurrency_stages: concurrencyStages.map((stage) => ({ concurrency: Number(stage.concurrency), num_requests: Number(stage.num_requests) })),
                                warmup_requests: Number(warmupRequests),
                                shared_prefix: sharedPrefixPayload,
                                workload_yaml: !matrixPoints.length && !sharedPrefixPayload ? normalizedWorkloadYaml : null,
                            },
                            scenarios: suiteProfileId === "custom" ? [] : (BUILT_IN_SUITES.find((item) => item.id === suiteProfileId)?.scenarios || [])
                                .filter((scenario) => suiteScenarioIds.includes(scenario.id))
                                .map((scenario) => suiteScenarioPayload(scenario, {
                                    ...suiteDefaults,
                                    parallelism: benchmark.parallelism,
                                    wait_timeout_seconds: benchmark.wait_timeout_seconds,
                                    warmup_requests: warmupRequests,
                                })),
                            include_baseline: baselineTypes.length > 0,
                            baseline_type: baselineTypes[0] || "direct-vllm",
                            baseline_types: baselineTypes,
                            preserve_deployment: preserveDeployment,
                        }); }),
                    }),
                });
            } else {
                const resolvedTargetId = resolveEvaluationTarget({ targetId });
                await api("/api/v1/evaluate/runs", {
                    method: "POST",
                    body: JSON.stringify({
                        deployment_execution_id: null,
                        model_service_group_id: resolvedTargetId,
                        harness: benchmark.harness,
                        workload: benchmark.workload,
                        parallelism: Number(benchmark.parallelism),
                        matrix: matrixPoints.map((point) => ({ isl: Number(point.isl), osl: Number(point.osl) })),
                        concurrency_stages: concurrencyStages.map((stage) => ({ concurrency: Number(stage.concurrency), num_requests: Number(stage.num_requests) })),
                        warmup_requests: Number(warmupRequests),
                        shared_prefix: sharedPrefixPayload,
                        workload_yaml: !matrixPoints.length && !sharedPrefixPayload ? normalizedWorkloadYaml : null,
                        api_key: apiToken.trim() || null,
                    }),
                });
            }
            setIsNewTaskOpen(false);
            setSelectedArtifacts([]);
            await loadAll();
        } catch (nextError) {
            setError(nextError.message);
        } finally {
            setBusy(false);
        }
    };

    const action = async (run, actionName) => {
        try {
            if (actionName === "delete" && !await confirmDelete(`Delete ${run.kind === "workflow" ? "evaluation" : "endpoint benchmark"} ${run.id}? This cannot be undone.`)) return;
            const collection = run.kind === "workflow" ? "workflow-runs" : "runs";
            await api(`/api/v1/evaluate/${collection}/${run.id}${actionName === "delete" ? "" : `/${actionName}`}`, { method: actionName === "delete" ? "DELETE" : "POST" });
            await loadAll({ quiet: true });
        } catch (nextError) {
            setError(nextError.message);
        }
    };

    return (
        <ModulePage contentClassName="space-y-5">
            <ModuleHeader
                icon={Gauge}
                title="Evaluation"
                description="Deploy, benchmark, and compare serving configurations with reproducible evidence."
                actions={<><button onClick={() => loadAll()} disabled={loading} title="Refresh" aria-label="Refresh evaluations" className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-slate-700 bg-slate-900 text-slate-300 hover:border-slate-600"><RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} /></button>{canCreateEvaluation && <button onClick={openNewTask} className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-sky-500 px-3 text-[10px] font-bold text-white hover:bg-sky-400"><Plus className="h-3.5 w-3.5" />New Task</button>}</>}
            />

            {error && <div className="relative flex items-center gap-2 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200"><AlertTriangle className="h-4 w-4 shrink-0" />{error}<button onClick={() => setError("")} className="ml-auto text-xs text-rose-300">Dismiss</button></div>}

            <section aria-label="Evaluation task summary" className="grid gap-2 rounded-xl border border-slate-900/80 bg-slate-900/40 p-2.5 sm:grid-cols-2 lg:grid-cols-[180px_repeat(5,minmax(0,1fr))]">
                <button type="button" onClick={() => setStatusFilter("")} className={`flex min-h-12 items-center justify-between rounded-lg border px-3.5 py-2 text-left transition ${!statusFilter ? "border-cyan-500/40 bg-slate-900 shadow-[0_0_12px_rgba(6,182,212,.08)]" : "border-slate-800 bg-slate-900/40 hover:border-slate-700"}`}><span><span className="block text-[9px] font-bold uppercase tracking-wider text-slate-400">Evaluation Tasks</span><span className="mt-0.5 block text-[8px] text-slate-600">All recorded runs</span></span><span className={`text-xl font-black ${!statusFilter ? "text-cyan-400" : "text-slate-300"}`}>{summary.total}</span></button>{[
                    ["succeeded", "Succeeded", statusCounts.succeeded, "text-emerald-300"],
                    ["running", "Running", statusCounts.running, "text-cyan-300"],
                    ["queued", "Queued", statusCounts.queued, "text-amber-300"],
                    ["failed", "Failed", statusCounts.failed, "text-rose-300"],
                    ["cancelled", "Cancelled", statusCounts.cancelled, "text-slate-300"],
                ].map(([key, label, value, color]) => <button key={key} onClick={() => setStatusFilter(statusFilter === key ? "" : key)} className={`flex min-h-12 items-center justify-between rounded-lg border px-3 py-2 text-left transition ${statusFilter === key ? "border-cyan-500/35 bg-cyan-500/5" : "border-slate-800 bg-slate-950/30 hover:border-slate-700"}`}><span className={`text-[9px] font-bold uppercase tracking-wide ${color}`}>{label}</span><span className="text-base font-black text-slate-300">{value}</span></button>)}
            </section>

            <section className="relative z-10 flex flex-col gap-3.5 rounded-3xl border border-slate-900/90 bg-[#070b13]/65 p-5 shadow-2xl backdrop-blur-md">
                <div className="relative z-20 flex flex-wrap items-center gap-3 rounded-2xl border border-slate-800/60 bg-[#0a0f1d] p-3 shadow-md"><div className="min-w-[240px] flex-1"><div className="relative"><Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" /><input type="search" placeholder="Search evaluation, workload, or harness..." value={searchInput} onChange={(event) => setSearchInput(event.target.value)} className="h-9 w-full rounded-xl border border-slate-800/60 bg-[#0b0f17] pl-9 pr-4 text-xs font-medium text-slate-200 outline-none focus:border-cyan-500/40" /></div></div><div className="w-48 shrink-0"><MultiSelectDropdown label="Mode" options={filterOptions.modes} selected={modeFilters} onChange={(value) => toggleFilter(setModeFilters, value)} /></div><div className="w-52 shrink-0"><MultiSelectDropdown label="Workload" options={filterOptions.workloads} selected={workloadFilters} onChange={(value) => toggleFilter(setWorkloadFilters, value)} /></div>{(statusFilter || searchInput || modeFilters.size || workloadFilters.size) && <button onClick={clearFilters} className="h-8 rounded-lg border border-slate-700 px-3 text-[10px] text-slate-300">Clear Filters</button>}</div>
                <div className="flex min-h-[52px] items-center justify-between border-b border-slate-800/60 px-4 py-2.5"><div className="flex items-center gap-2"><BarChart3 className="h-4 w-4 text-cyan-400" /><h2 className="text-sm font-bold text-slate-300">Evaluation evidence</h2><span className="rounded-md bg-slate-900 px-2 py-0.5 text-[9px] text-slate-500">{filteredTasks.length} shown</span></div><span className="text-[10px] text-slate-600">{summary.comparisons} multi-configuration comparison{summary.comparisons === 1 ? "" : "s"}</span></div>
                {loading && !tasks.length ? <div className="space-y-2">{[0, 1, 2].map((item) => <div key={item} className="h-32 animate-pulse rounded-lg border border-slate-800 bg-slate-900/50" />)}</div> : filteredTasks.length ? <><div className="flex flex-col gap-2">{pagedTasks.map((task) => <TaskCard key={`${task.kind}-${task.id}`} task={task} onOpen={() => onNavigate("optimization-evaluation-details", { evaluationId: task.id, runKind: task.kind })} onAction={action} />)}</div><div className="flex flex-col gap-3 border-t border-slate-800/60 px-2 pt-4 sm:flex-row sm:items-center sm:justify-between"><div className="flex items-center gap-2 text-[10px] text-slate-500"><span>Showing {(taskPage - 1) * taskPageSize + 1}–{Math.min(taskPage * taskPageSize, filteredTasks.length)} of {filteredTasks.length}</span><select aria-label="Tasks per page" value={taskPageSize} onChange={(event) => setTaskPageSize(Number(event.target.value))} className="h-8 rounded-lg border border-slate-800 bg-slate-950 px-2 text-[10px] text-slate-300 outline-none focus:border-cyan-500/40"><option value={5}>5 / page</option><option value={10}>10 / page</option><option value={20}>20 / page</option></select></div><div className="flex items-center gap-1"><PaginationControls page={taskPage - 1} totalPages={taskPageCount} onPageChange={(page) => setTaskPage(page + 1)} /></div></div></> : <div className="rounded-2xl border border-slate-800 bg-slate-900/80 px-6 py-12 text-center"><CircleDashed className="mx-auto h-7 w-7 text-slate-600" /><h3 className="mt-3 text-sm font-semibold">No matching evaluation tasks</h3><p className="mt-2 text-xs text-slate-500">Adjust filters or create a new task.</p></div>}
            </section>

            <Modal isOpen={isNewTaskOpen} onClose={() => setIsNewTaskOpen(false)} size="xl" className="!max-w-4xl !border-cyan-500/20 !bg-[#070b13] shadow-[0_24px_80px_rgba(2,6,23,.72)]" title={<div><p className="text-[8px] font-bold uppercase tracking-[0.22em] text-cyan-400">Evaluation workflow</p><h2 className="mt-0.5 text-lg font-bold text-slate-100">Create a new task</h2></div>} subtitle="Choose a target, configure traffic, then launch a reproducible benchmark.">
                <div ref={wizardTopRef} className="relative mx-auto min-w-0 max-w-4xl scroll-mt-4 space-y-4 overflow-x-hidden"><div className="pointer-events-none absolute -right-20 -top-20 h-52 w-52 rounded-full bg-cyan-500/[0.05] blur-3xl" />
                    <ol className="relative grid grid-cols-2 gap-2" aria-label="Task creation progress">{["Configuration", "Benchmark & comparison"].map((label, index) => <li key={label} className="min-w-0"><button type="button" disabled={index > wizardStep} onClick={() => goToWizardStep(index)} className="w-full text-left disabled:cursor-not-allowed"><div className={`h-1.5 rounded-full transition-colors ${index <= wizardStep ? "bg-gradient-to-r from-cyan-400 to-blue-500 shadow-[0_0_12px_rgba(34,211,238,.3)]" : "bg-slate-800"}`} /><div className={`mt-2 text-[9px] font-bold uppercase tracking-wider ${index === wizardStep ? "text-cyan-300" : index < wizardStep ? "text-slate-400 hover:text-cyan-200" : "text-slate-600"}`}>{index + 1}. {label}</div></button></li>)}</ol>
                    {wizardStep === 0 && <>
                    <section className="relative rounded-xl border border-slate-800/80 bg-[#0a0f1b]/80 p-3"><div className="mb-3 flex items-center gap-2.5"><span className="flex h-6 w-6 items-center justify-center rounded-md border border-cyan-500/25 bg-cyan-500/10 text-[9px] font-black text-cyan-300">01</span><div><h3 className="text-xs font-bold text-slate-100">Choose evaluation target</h3><p className="text-[9px] text-slate-500">Deploy configurations for comparison or benchmark a live endpoint.</p></div></div><div className="grid gap-2 md:grid-cols-2">
                        <button type="button" onClick={() => setMode("deploy")} className={`group rounded-lg border p-3 text-left transition-all ${mode === "deploy" ? "border-cyan-400/60 bg-gradient-to-r from-cyan-500/12 to-blue-500/5 shadow-[0_8px_24px_rgba(34,211,238,.08)]" : "border-slate-800 bg-slate-950/40 hover:border-slate-700"}`}><div className="flex items-center gap-2"><div className="rounded-md border border-cyan-500/20 bg-cyan-400/10 p-1.5 text-cyan-300"><Workflow className="h-4 w-4" /></div><h3 className="flex-1 text-xs font-semibold text-slate-100">Deploy configurations</h3>{mode === "deploy" && <CheckCircle2 className="h-4 w-4 text-cyan-300" />}</div><p className="mt-2 text-[9px] leading-4 text-slate-500">Compare immutable configurations on their bound cluster.</p></button>
                        <button type="button" onClick={() => setMode("existing")} className={`group rounded-lg border p-3 text-left transition-all ${mode === "existing" ? "border-cyan-400/60 bg-gradient-to-r from-cyan-500/12 to-blue-500/5 shadow-[0_8px_24px_rgba(34,211,238,.08)]" : "border-slate-800 bg-slate-950/40 hover:border-slate-700"}`}><div className="flex items-center gap-2"><div className="rounded-md border border-cyan-500/20 bg-cyan-400/10 p-1.5 text-cyan-300"><Server className="h-4 w-4" /></div><h3 className="flex-1 text-xs font-semibold text-slate-100">Use existing endpoint</h3>{mode === "existing" && <CheckCircle2 className="h-4 w-4 text-cyan-300" />}</div><p className="mt-2 text-[9px] leading-4 text-slate-500">Benchmark a discovered live service without deploying.</p></button>
                    </div></section>

                    {mode === "deploy" ? <section className="space-y-3">
                        <div className="flex items-center justify-between border-b border-slate-800"><div className="flex gap-1"><button onClick={() => setConfigurationTab("select")} className={`border-b-2 px-3 py-2 text-[10px] font-semibold uppercase tracking-wide ${configurationTab === "select" ? "border-cyan-400 text-cyan-200" : "border-transparent text-slate-500"}`}>Select</button><button onClick={() => setConfigurationTab("yaml")} disabled={!inspectedArtifactId} className={`border-b-2 px-3 py-2 text-[10px] font-semibold uppercase tracking-wide disabled:opacity-30 ${configurationTab === "yaml" ? "border-cyan-400 text-cyan-200" : "border-transparent text-slate-500"}`}>YAML</button></div><button onClick={goToConfiguration} className="inline-flex items-center gap-1 rounded-lg border border-cyan-500/30 px-3 py-1.5 text-[10px] text-cyan-200"><Plus className="h-3 w-3" />Create configuration</button></div>
                        {configurationTab === "select" ? <><div><h3 className="text-sm font-semibold">Deployment configurations</h3><p className="mt-1 text-[10px] text-slate-500">Each card identifies the artifact, deployment variant, topology, and cluster without opening the YAML.</p></div><div className="max-h-64 space-y-2 overflow-y-auto pr-1">{visibleArtifacts.length ? visibleArtifacts.map((artifact) => { const details = artifactDetails(artifact); const selected = selectedArtifacts.includes(artifact.artifact_id); return <div key={artifact.artifact_id} className={`flex items-center gap-2 rounded-xl border p-2.5 ${selected ? "border-cyan-400/50 bg-cyan-400/[0.08]" : "border-slate-800 bg-slate-900/30"}`}><button type="button" onClick={() => toggleArtifact(artifact.artifact_id)} className="flex min-w-0 flex-1 items-center gap-3 text-left"><span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-md border ${selected ? "border-cyan-400 bg-cyan-400 text-slate-950" : "border-slate-600"}`}>{selected && <CheckCircle2 className="h-3.5 w-3.5" />}</span><span className="min-w-0 flex-1"><span className="block truncate font-mono text-[10px] font-semibold text-cyan-200" title={details.name}>{details.name}</span><span className="mt-1 flex flex-wrap items-center gap-1.5 text-[9px]"><span className="rounded border border-slate-700 px-1.5 py-0.5 text-slate-300">{details.guide}</span>{details.variant && <span className="rounded border border-violet-500/30 bg-violet-500/10 px-1.5 py-0.5 font-mono text-violet-200">{details.variant}</span>}<span className="rounded border border-slate-800 px-1.5 py-0.5 text-slate-400">{details.topology}</span></span><span className="mt-1 block truncate text-[9px] text-slate-600">{details.model} · cluster {details.cluster}</span></span></button><button onClick={() => inspectArtifact(artifact.artifact_id)} className="inline-flex items-center gap-1 rounded-lg border border-slate-700 px-2 py-1.5 text-[9px] text-slate-400 hover:text-cyan-200"><Code2 className="h-3 w-3" />View YAML</button><button title="Delete saved YAML" onClick={() => deleteArtifact(artifact.artifact_id)} className="rounded-lg border border-red-500/25 p-1.5 text-red-300 hover:bg-red-500/10"><Trash2 className="h-3 w-3" /></button></div>; }) : <div className="rounded-xl border border-dashed border-slate-700 p-6 text-center text-xs text-slate-500">No selected or successfully deployed configurations. Create one to continue.</div>}</div>{selectedArtifacts.length > 0 && <div className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-[10px] ${selectedClusterSessions.length === 1 ? "border-emerald-500/25 bg-emerald-500/[0.06] text-emerald-200" : "border-amber-500/25 bg-amber-500/[0.06] text-amber-200"}`}><Server className="h-3.5 w-3.5" />Deploy target: {selectedClusterNames.join(", ") || "No bound cluster"}{selectedClusterSessions.length > 1 && " · configurations must use the same cluster"}</div>}</> : <div className="space-y-3"><div className="flex flex-wrap items-center justify-between gap-2"><div><h3 className="text-sm font-semibold">Configuration manifest</h3><p className="mt-1 text-[10px] text-slate-500">Edit the manifest and save it as a new immutable Configuration. The original remains unchanged.</p></div><div className="flex gap-2"><label className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-slate-700 px-2 py-1.5 text-[9px] text-slate-400"><Upload className="h-3 w-3" />Load YAML<input type="file" accept=".yaml,.yml,text/yaml" className="hidden" onChange={(event) => { const file = event.target.files?.[0]; if (file) file.text().then(setYamlText); }} /></label><button onClick={saveYamlVariant} disabled={yamlSaving || yamlLoading || !yamlText.trim()} className="rounded-lg bg-cyan-400 px-3 py-1.5 text-[9px] font-semibold text-slate-950 disabled:opacity-40">{yamlSaving ? "Saving…" : "Save as new configuration"}</button></div></div>{yamlLoading ? <div className="h-56 animate-pulse rounded-xl bg-slate-900/60" /> : <textarea value={yamlText} onChange={(event) => setYamlText(event.target.value)} spellCheck={false} className="h-64 w-full resize-y rounded-xl border border-slate-700 bg-black/30 p-3 font-mono text-[10px] leading-5 text-slate-300 outline-none focus:border-cyan-500" />}</div>}
                    </section> : <section className="space-y-3"><label className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Model service<select className={inputClass} value={targetId} onChange={(event) => setTargetId(event.target.value)}><option value="">Select a published model service</option>{modelServices.map((item) => <option key={item.id} value={item.id}>{item.name}{item.baseModel ? ` · ${item.baseModel}` : ""}</option>)}</select>{!modelServices.length && <p className="mt-2 flex items-start gap-1.5 text-[10px] leading-4 text-amber-300"><Info className="mt-0.5 h-3 w-3 shrink-0" />No model service is published and healthy yet. Publish one from the Model Service page first.</p>}</label><label className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Model access token<input type="password" autoComplete="off" placeholder="lens-mk-…" className={inputClass} value={apiToken} onChange={(event) => setApiToken(event.target.value)} /><p className="mt-1 text-[9px] text-slate-500">Required: the harness authenticates through the cluster&apos;s shared Gateway. Used for this run only; never stored.</p></label></section>}
                    </>}

                    {wizardStep === 1 && <section className="rounded-xl border border-slate-800/80 bg-[#0a0f1b]/80 p-3">
                        <div className="mb-3 flex items-center gap-2.5"><span className="flex h-6 w-6 items-center justify-center rounded-md border border-emerald-500/25 bg-emerald-500/10 text-[9px] font-black text-emerald-300">02</span><div><h3 className="text-xs font-bold">Benchmark suite</h3><p className="text-[9px] text-slate-500">Apply the same reusable traffic scenarios to every selected Guide configuration.</p></div></div>
                        <div className="mb-3 rounded-lg border border-slate-800 bg-slate-950/45 p-3">
                            <div className="grid gap-2 md:grid-cols-3 lg:grid-cols-5">
                                <button type="button" onClick={() => selectSuiteProfile("custom")} className={`rounded-lg border p-2.5 text-left ${suiteProfileId === "custom" ? "border-cyan-400/50 bg-cyan-400/10" : "border-slate-800"}`}><span className="block text-[11px] font-semibold text-slate-200">Custom workload</span><span className="mt-1 block text-[9px] text-slate-500">Matrix, shared prefix, or YAML.</span></button>
                                {BUILT_IN_SUITES.map((profile) => <button key={profile.id} type="button" onClick={() => selectSuiteProfile(profile.id)} className={`rounded-lg border p-2.5 text-left ${suiteProfileId === profile.id ? "border-cyan-400/50 bg-cyan-400/10" : "border-slate-800"}`}><span className="block text-[11px] font-semibold text-slate-200">{profile.name}</span><span className="mt-1 block text-[9px] leading-4 text-slate-500">{profile.description}</span></button>)}
                            </div>
                            {suiteProfileId !== "custom" && <div className="mt-3 grid gap-3 border-t border-slate-800 pt-3 lg:grid-cols-[1fr_2fr]">
                                <div className="grid grid-cols-2 gap-2">
                                    <label className="text-[9px] uppercase tracking-wide text-slate-500">Default input tokens<input type="number" min="1" className={inputClass} value={suiteDefaults.isl} onChange={(event) => setSuiteDefaults((current) => ({ ...current, isl: event.target.value }))} /></label>
                                    <label className="text-[9px] uppercase tracking-wide text-slate-500">Default output tokens<input type="number" min="1" className={inputClass} value={suiteDefaults.osl} onChange={(event) => setSuiteDefaults((current) => ({ ...current, osl: event.target.value }))} /></label>
                                    <label className="text-[9px] uppercase tracking-wide text-slate-500">Default concurrency<input type="number" min="1" className={inputClass} value={suiteDefaults.concurrency} onChange={(event) => setSuiteDefaults((current) => ({ ...current, concurrency: event.target.value }))} /></label>
                                    <label className="text-[9px] uppercase tracking-wide text-slate-500">Default requests<input type="number" min="1" className={inputClass} value={suiteDefaults.requests} onChange={(event) => setSuiteDefaults((current) => ({ ...current, requests: event.target.value }))} /></label>
                                </div>
                                <div><p className="mb-2 text-[9px] font-semibold uppercase tracking-wide text-slate-500">Scenarios</p><div className="grid gap-2 sm:grid-cols-2">{(BUILT_IN_SUITES.find((item) => item.id === suiteProfileId)?.scenarios || []).map((scenario) => <label key={scenario.id} className={`flex cursor-pointer items-start gap-2 rounded border p-2 ${suiteScenarioIds.includes(scenario.id) ? "border-emerald-500/30 bg-emerald-500/[0.06]" : "border-slate-800 opacity-60"}`}><input type="checkbox" checked={suiteScenarioIds.includes(scenario.id)} onChange={() => setSuiteScenarioIds((current) => current.includes(scenario.id) ? current.filter((id) => id !== scenario.id) : [...current, scenario.id])} className="mt-0.5 accent-emerald-400" /><span><span className="block text-[10px] font-semibold text-slate-300">{scenario.name}</span><span className="mt-0.5 block text-[9px] text-slate-600">{scenario.isl || suiteDefaults.isl}→{scenario.osl || suiteDefaults.osl} tokens · {scenario.concurrencyStages?.length ? `${scenario.concurrencyStages.length} load stages` : `C${scenario.concurrency || suiteDefaults.concurrency} · ${scenario.requests || suiteDefaults.requests} requests`}</span></span></label>)}</div><p className="mt-2 text-[9px] text-cyan-300">{selectedArtifacts.length || 1} configuration(s) × {suiteScenarioIds.length} scenario(s) = {(selectedArtifacts.length || 1) * suiteScenarioIds.length} candidate benchmark case(s), plus selected baselines.</p></div>
                            </div>}
                        </div>
                        {suiteProfileId !== "custom" && <div className="mb-3 rounded-lg border border-emerald-500/20 bg-emerald-500/[0.05] px-3 py-2 text-[10px] text-emerald-200">Built-in scenarios use exact-length inference-perf workloads. The custom controls below are preserved and will be used again when Custom workload is selected.</div>}
                        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4"><label className="text-xs text-slate-400">Task name<input className={inputClass} value={name} onChange={(event) => setName(event.target.value)} /></label><label className="text-xs text-slate-400">Benchmark runner<select className={inputClass} value={benchmark.harness} onChange={(event) => setBenchmark((current) => ({ ...current, harness: event.target.value }))}><option>inference-perf</option><option>guidellm</option><option>vllm-benchmark</option></select></label><div><label className="text-xs text-slate-400">Traffic profile name<input className={inputClass} value={benchmark.workload} onChange={(event) => setBenchmark((current) => ({ ...current, workload: event.target.value }))} /></label><button onClick={() => setWorkloadExpanded((current) => !current)} className="mt-1 inline-flex items-center gap-1 text-[9px] text-cyan-300"><FileCode2 className="h-3 w-3" />{workloadExpanded ? "Hide YAML editor" : "Edit traffic YAML"}</button></div><label className="text-xs text-slate-400">Traffic workers<input type="number" min="1" max="32" className={inputClass} value={benchmark.parallelism} onChange={(event) => setBenchmark((current) => ({ ...current, parallelism: event.target.value }))} /></label></div>{workloadExpanded && <div className="mt-3 grid gap-3 rounded-xl border border-slate-700 bg-black/20 p-3 lg:grid-cols-[260px_1fr]"><div><h4 className="text-xs font-semibold text-slate-200">Editable traffic profile</h4><p className="mt-2 text-[10px] leading-5 text-slate-500">Edit or upload the YAML used for this task. It is validated by the API, stored only inside this benchmark run, and passed through <span className="font-mono">--workload-file-path</span>.</p><label className="mt-3 inline-flex cursor-pointer items-center gap-1 border border-slate-700 px-2 py-1.5 text-[9px] text-slate-300"><Upload className="h-3 w-3" />Load YAML<input type="file" accept=".yaml,.yml,text/yaml" className="hidden" onChange={(event) => { const file = event.target.files?.[0]; if (file) file.text().then(setWorkloadYaml); event.target.value = ""; }} /></label><p className="mt-2 text-[9px] text-amber-300">Matrix or shared-prefix mode overrides this YAML.</p></div><textarea aria-label="Editable benchmark traffic YAML" value={workloadYaml} onChange={(event) => setWorkloadYaml(event.target.value)} spellCheck={false} className="h-64 w-full resize-y rounded-lg border border-slate-800 bg-slate-950 p-3 font-mono text-[9px] leading-4 text-slate-300 outline-none focus:border-cyan-500" /></div>}
                        {benchmark.harness === "inference-perf" && <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/45 p-3">
                            <div className="flex items-center justify-between gap-3">
                                <div>
                                    <p className="text-xs font-semibold text-slate-300">ISL × OSL × concurrency matrix (optional)</p>
                                    <p className="mt-0.5 text-[9px] text-slate-600">When set, each row below runs as its own exact-length benchmark instead of the traffic profile above; the concurrency stages run together within each row.</p>
                                </div>
                                <button type="button" onClick={() => setMatrixExpanded((current) => !current)} className="shrink-0 border border-slate-700 px-2 py-1 text-[9px] text-slate-300 hover:border-cyan-500/40 hover:text-cyan-200">{matrixExpanded ? "Hide matrix" : matrixPoints.length ? `${matrixPoints.length} point(s)` : "Add matrix"}</button>
                            </div>
                            {matrixExpanded && <div className="mt-3 space-y-3">
                                <div>
                                    <p className="mb-1 text-[9px] uppercase tracking-wide text-slate-500">Input/output length points (tokens)</p>
                                    {matrixPoints.map((point, index) => <div key={index} className="mb-1.5 flex items-center gap-2">
                                        <input type="number" min="1" placeholder="ISL" className={`${inputClass} mt-0`} value={point.isl} onChange={(event) => setMatrixPoints((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, isl: event.target.value } : item))} />
                                        <input type="number" min="1" placeholder="OSL" className={`${inputClass} mt-0`} value={point.osl} onChange={(event) => setMatrixPoints((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, osl: event.target.value } : item))} />
                                        <button type="button" onClick={() => setMatrixPoints((current) => current.filter((_, itemIndex) => itemIndex !== index))} className="shrink-0 text-slate-500 hover:text-red-300"><Trash2 className="h-3.5 w-3.5" /></button>
                                    </div>)}
                                    <button type="button" onClick={() => { setRateStages([]); setMatrixPoints((current) => [...current, { isl: 1024, osl: 128 }]); }} className="mt-1 inline-flex items-center gap-1 text-[9px] text-cyan-300"><Plus className="h-3 w-3" />Add ISL/OSL point</button>
                                </div>
                                <div>
                                    <p className="mb-1 text-[9px] uppercase tracking-wide text-slate-500">Concurrency stages (leave empty for the default 1/8/32/64 sweep)</p>
                                    {concurrencyStages.map((stage, index) => <div key={index} className="mb-1.5 flex items-center gap-2">
                                        <input type="number" min="1" placeholder="Concurrency" className={`${inputClass} mt-0`} value={stage.concurrency} onChange={(event) => setConcurrencyStages((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, concurrency: event.target.value } : item))} />
                                        <input type="number" min="1" placeholder="Requests" className={`${inputClass} mt-0`} value={stage.num_requests} onChange={(event) => setConcurrencyStages((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, num_requests: event.target.value } : item))} />
                                        <button type="button" onClick={() => setConcurrencyStages((current) => current.filter((_, itemIndex) => itemIndex !== index))} className="shrink-0 text-slate-500 hover:text-red-300"><Trash2 className="h-3.5 w-3.5" /></button>
                                    </div>)}
                                    <button type="button" onClick={() => setConcurrencyStages((current) => [...current, { concurrency: 1, num_requests: 32 }])} className="mt-1 inline-flex items-center gap-1 text-[9px] text-cyan-300"><Plus className="h-3 w-3" />Add concurrency stage</button>
                                </div>
                                <label className="block text-xs text-slate-400">Warm-up requests (uncounted, run once before the sweep)<input type="number" min="0" max="50" className={inputClass} value={warmupRequests} onChange={(event) => setWarmupRequests(event.target.value)} /><span className="mt-1 block text-[9px] text-slate-600">Absorbs JIT/torch-compile cold-start cost so the first measured point isn't unfairly slower. Set to 0 to disable.</span></label>
                            </div>}
                        </div>}
                        <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/45 p-3">
                            <div className="flex items-center justify-between gap-3">
                                <div>
                                    <p className="text-xs font-semibold text-slate-300">Shared-prefix workload generator (optional)</p>
                                    <p className="mt-0.5 text-[9px] text-slate-600">Guide-independent inference-perf workload: reuse system prompts while changing the open-loop request rate. It works with any OpenAI-compatible deployment, but is mainly useful for cache routing/offload studies. It replaces the YAML profile and is mutually exclusive with the matrix.</p>
                                </div>
                                <button type="button" onClick={() => setSharedPrefixExpanded((current) => !current)} className="shrink-0 border border-slate-700 px-2 py-1 text-[9px] text-slate-300 hover:border-cyan-500/40 hover:text-cyan-200">{sharedPrefixExpanded ? "Hide rate ramp" : rateStages.length ? `${rateStages.length} stage(s)` : "Add rate ramp"}</button>
                            </div>
                            {sharedPrefixExpanded && <div className="mt-3 space-y-3">
                                <div className="grid gap-2 md:grid-cols-3">
                                    <label className="text-xs text-slate-400">Shared-prompt groups<input type="number" min="1" className={inputClass} value={sharedPrefixConfig.num_groups} onChange={(event) => setSharedPrefixConfig((current) => ({ ...current, num_groups: event.target.value }))} /></label>
                                    <label className="text-xs text-slate-400">Prompts per group<input type="number" min="1" className={inputClass} value={sharedPrefixConfig.num_prompts_per_group} onChange={(event) => setSharedPrefixConfig((current) => ({ ...current, num_prompts_per_group: event.target.value }))} /></label>
                                    <label className="text-xs text-slate-400">System prompt length (tokens)<input type="number" min="1" className={inputClass} value={sharedPrefixConfig.system_prompt_len} onChange={(event) => setSharedPrefixConfig((current) => ({ ...current, system_prompt_len: event.target.value }))} /></label>
                                    <label className="text-xs text-slate-400">Question length (tokens)<input type="number" min="1" className={inputClass} value={sharedPrefixConfig.question_len} onChange={(event) => setSharedPrefixConfig((current) => ({ ...current, question_len: event.target.value }))} /></label>
                                    <label className="text-xs text-slate-400">Output length (tokens)<input type="number" min="1" className={inputClass} value={sharedPrefixConfig.output_len} onChange={(event) => setSharedPrefixConfig((current) => ({ ...current, output_len: event.target.value }))} /></label>
                                </div>
                                <label className="flex items-center gap-2 text-xs text-slate-400"><input type="checkbox" checked={sharedPrefixConfig.enable_multi_turn_chat} onChange={(event) => setSharedPrefixConfig((current) => ({ ...current, enable_multi_turn_chat: event.target.checked }))} className="accent-cyan-400" />Enable multi-turn chat</label>
                                <div>
                                    <p className="mb-1 text-[9px] uppercase tracking-wide text-slate-500">Rate stages (target requests/second, ramped in order)</p>
                                    {rateStages.map((stage, index) => <div key={index} className="mb-1.5 flex items-center gap-2">
                                        <input type="number" min="0" step="0.1" placeholder="Rate (req/s)" className={`${inputClass} mt-0`} value={stage.rate} onChange={(event) => setRateStages((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, rate: event.target.value } : item))} />
                                        <input type="number" min="1" placeholder="Duration (s)" className={`${inputClass} mt-0`} value={stage.duration} onChange={(event) => setRateStages((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, duration: event.target.value } : item))} />
                                        <button type="button" onClick={() => setRateStages((current) => current.filter((_, itemIndex) => itemIndex !== index))} className="shrink-0 text-slate-500 hover:text-red-300"><Trash2 className="h-3.5 w-3.5" /></button>
                                    </div>)}
                                    <button type="button" onClick={() => { setMatrixPoints([]); setRateStages((current) => [...current, { rate: 1, duration: 60 }]); }} className="mt-1 inline-flex items-center gap-1 text-[9px] text-cyan-300"><Plus className="h-3 w-3" />Add rate stage</button>
                                </div>
                            </div>}
                        </div>
                        {mode === "deploy" && <ComparisonPlan guides={comparisonGuideOptions} selectedGuides={comparisonGuides} selectedArtifacts={selectedArtifactObjects} parameters={comparisonParameters} editingGuide={editingComparisonGuide} includeRawVllm={includeRawVllm} needsKubernetesServiceComparison={needsKubernetesServiceComparison} onToggleGuide={toggleComparisonGuide} onEditGuide={setEditingComparisonGuide} onUpdateParameter={updateComparisonParameter} onToggleRawVllm={() => setIncludeRawVllm((current) => !current)} />}
                        {mode === "deploy" && <label className={`mt-3 flex cursor-pointer items-start gap-3 rounded-xl border p-3 ${preserveDeployment ? "border-cyan-400/40 bg-cyan-400/[0.07]" : "border-slate-800 bg-slate-950/40"}`}><input type="checkbox" checked={preserveDeployment} onChange={(event) => setPreserveDeployment(event.target.checked)} className="mt-0.5 accent-cyan-400" /><span><span className="block text-xs font-semibold text-slate-200">Keep deployment namespace after evaluation</span><span className="mt-1 block text-[10px] leading-4 text-slate-500">Preserve the generated namespace and ready endpoint for Monitoring. Cleanup must be performed manually later.</span></span></label>}
                    </section>}

                    <div ref={wizardNextRef} className="sticky -bottom-4 z-20 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-700/70 bg-[#090e18]/95 p-2.5 shadow-[0_-10px_30px_rgba(2,6,23,.6)] backdrop-blur-xl"><div className="flex items-center gap-2"><span className={`h-2 w-2 rounded-full ${mode === "deploy" ? selectedArtifacts.length && selectedClusterSessions.length === 1 ? "bg-emerald-400 shadow-[0_0_8px_#34d399]" : "bg-amber-400" : targetId && apiToken.trim() ? "bg-emerald-400 shadow-[0_0_8px_#34d399]" : "bg-amber-400"}`} /><p className="text-[10px] text-slate-400">{mode === "deploy" ? `${selectedArtifacts.length} configuration${selectedArtifacts.length === 1 ? "" : "s"} selected` : targetId ? (apiToken.trim() ? "Endpoint ready" : "Enter a model access token") : "Select a model service"}</p></div><div className="flex gap-2"><button onClick={() => setIsNewTaskOpen(false)} className="h-8 rounded-md border border-slate-700 px-3 text-[10px] text-slate-300 hover:bg-slate-800">Cancel</button>{wizardStep === 1 && <button onClick={() => goToWizardStep(0)} className="inline-flex h-8 items-center gap-1 rounded-md border border-slate-700 px-3 text-[10px] text-slate-300 hover:bg-slate-800"><ChevronLeft className="h-3.5 w-3.5" />Back</button>}{wizardStep === 0 ? <button disabled={mode === "deploy" ? !selectedArtifacts.length || selectedClusterSessions.length !== 1 : !targetId || !apiToken.trim()} onClick={() => goToWizardStep(1)} className="inline-flex h-8 items-center gap-1.5 rounded-md bg-sky-500 px-4 text-[10px] font-bold text-white hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-40">Next<ChevronRight className="h-3.5 w-3.5" /></button> : <button disabled={busy} onClick={createTask} className="inline-flex h-8 items-center gap-1.5 rounded-md bg-sky-500 px-4 text-[10px] font-bold text-white shadow-[0_6px_18px_rgba(14,165,233,.2)] hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-40">{busy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}Launch evaluation</button>}</div></div>
                </div>
            </Modal>
        </ModulePage>
    );
}
