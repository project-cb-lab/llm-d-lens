import { resolveEvaluationTarget } from '../features/evaluation/client';
import { editedEvaluationConfiguration } from '../features/evaluation/configuration';
import OptimizationSelectionSummary from './evaluation/OptimizationSelectionSummary.jsx';
import { selectedOptimizationPlan } from '../features/evaluation/experimentDesign.js';
import { confirmDelete } from "./ui/confirmDelete";
import { useEffect, useRef, useState } from "react";
import {
    AlertTriangle, ArrowLeft, Boxes, Check, ChevronDown, ChevronLeft,
    ChevronRight, Code2, Cpu, Ellipsis, ExternalLink, GitCompareArrows, Layers3, Pencil, Play, Plus, RefreshCw, Server, Settings2, SlidersHorizontal, Trash2,
} from "lucide-react";
import EvaluationConfigurationEditor from "./EvaluationConfigurationEditor";
import BenchmarkInputs from "./evaluation/BenchmarkInputs";
import EvaluationPlan from "./evaluation/EvaluationPlan";
import { benchmarkIssues, benchmarkScenario, configurationContextLimit } from "../features/evaluation/benchmarkSettings";
import { Badge, Button, Modal } from "./ui";
import { StorageVolumeSelect } from "./common/StorageVolumeSelect";
import { listStorageVolumes } from "./StorageManagement/storageManagementBackend";
import { listModelCacheEntries } from "./ModelCache/modelCacheBackend";
import { loadCluster, loadClusterOverview, loadClusters, selectCluster } from "./OptimizationWorkspace/clusterBackend";
import { deleteConfigurationArtifact, loadConfigurationArtifacts, loadConfigurationCapabilities, saveConfiguration } from "./OptimizationWorkspace/configurationBackend";
import { startLocalDeployment } from "./OptimizationWorkspace/remoteDeployBackend";
import { listModelNames } from "./ModelService/modelServiceBackend";
import { loadGuideCatalog } from "./OptimizationWorkspace/guidePlanningBackend";
import { createBenchmarkRun, createEvaluation } from "../features/evaluation/client";
import {
    applyRecommendedWorkload, benchmarkWorkloadMode, configurationSummary,
} from "../features/evaluation/capabilities";
import { comparisonConfigurationDraft } from "../features/evaluation/configuration";
import { matchesEvaluationSetup, retainCompatibleConfigurations } from "../features/evaluation/setup";
import { clearEvaluationIntent, readEvaluationIntent, storeEvaluationIntent } from "../features/evaluation/transfer";
import { useAuth } from "../features/auth/useAuth";
import { MODELS } from "../data/modelCatalog";
import { acceleratorVariantForHardware, DEFAULT_RUNTIME_IMAGES, isDefaultRuntimeImage } from "./benchmark-results/acceleratorDisplay";

const STEPS = ["Evaluation Setup", "Configurations", "Benchmark", "Execution plan"];
const inputClass = "mt-1 h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-xs text-slate-100 outline-none focus:border-cyan-400";

function artifactCluster(artifact) {
    return artifact?.deployable_configuration?.provenance?.cluster_ref || {};
}

function artifactContent(artifact) {
    return artifact?.deployable_configuration?.content || {};
}

function benchmarkFor(provider) {
    return applyRecommendedWorkload({
        harness: "inference-perf",
        workload: "sanity_random.yaml",
        parallelism: 1,
        wait_timeout_seconds: 1800,
        warmup_requests: 2,
        matrix: [],
        concurrency_stages: [],
        shared_prefix: null,
    }, provider);
}

function defaultTaskName(guide) {
    const label = guide?.label || "Evaluation";
    return `${label} · ${new Date().toISOString().replace(/\D/g, "")}`;
}

function timestampSuffix() {
    return new Date().toISOString().replace(/\D/g, "").slice(0, 14);
}

function deploymentServiceName(base, artifact, index, suffix) {
    const guide = artifact?.deployable_configuration?.provider_ref || `config-${index + 1}`;
    return `${base}-${guide}-${index + 1}-${suffix}`
        .toLowerCase()
        .replace(/[^a-z0-9-]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 63);
}

function SectionLabel({ children }) {
    return <p className="text-[9px] font-bold uppercase tracking-[0.18em] text-slate-500">{children}</p>;
}

function configurationFacts(artifact, provider) {
    const summary = configurationSummary(artifact, provider);
    const content = artifact?.deployable_configuration?.content || {};
    const decode = content.decode || content.serving || {};
    const prefill = content.prefill || null;
    const replicas = Number(decode.replicaCount ?? decode.replicas ?? 0);
    const tp = Number(decode.tensorParallelSize ?? decode.tensor_parallel_size ?? 0);
    const prefillReplicas = Number(prefill?.replicaCount ?? prefill?.replicas ?? 0);
    const prefillTp = Number(prefill?.tensorParallelSize ?? prefill?.tensor_parallel_size ?? 0);
    return {
        ...summary,
        replicas,
        tp,
        prefillReplicas,
        prefillTp,
        gpu: replicas * tp + prefillReplicas * prefillTp,
        runtime: content.runtime?.modelServer || content.runtime?.backend || "vLLM",
        blockSize: content.customParameters?.find?.((item) => item.name === "block-size")?.value
            || content.custom_parameters?.find?.((item) => item.name === "block-size")?.value,
    };
}

export default function EvaluationTaskWizard({ onNavigate }) {
    // Snapshot the hand-off once. The hidden Evaluation task list mounted by App
    // consumes the same session key after this screen renders; reading it again on
    // subsequent renders would silently turn a Model Market deploy-only flow back
    // into the normal benchmark wizard.
    const [returningIntent] = useState(() => readEvaluationIntent());
    const deployOnly = returningIntent?.operation === "deploy-only";
    const { can } = useAuth();
    // A user without the deploy-capable evaluation permission may still benchmark
    // an existing (own/shared) deployment, but cannot design configurations
    // because that path deploys a new one.
    const benchmarkOnly = !deployOnly && !(can("evaluate:workflow:create") || can("deployment:run:create")) && can("evaluate:run:create");
    const [step, setStep] = useState(deployOnly ? 1 : 0);
    const [name, setName] = useState(returningIntent?.name || "");
    const [template, setTemplate] = useState(returningIntent?.workloads?.[0]?.guide || "");
    const [catalog, setCatalog] = useState([]);
    const [editorResources, setEditorResources] = useState(null);
    const [editorBusy, setEditorBusy] = useState(false);
    const [configurationNotice, setConfigurationNotice] = useState("");
    const [providers, setProviders] = useState(new Map());
    const [artifacts, setArtifacts] = useState([]);
    const [artifactIds, setArtifactIds] = useState([]);
    const [configurationSources, setConfigurationSources] = useState({});
    const [openMenuId, setOpenMenuId] = useState("");
    const [cloningArtifactId, setCloningArtifactId] = useState("");
    const [yamlArtifact, setYamlArtifact] = useState(null);
    const [yamlText, setYamlText] = useState("");
    const [yamlLoading, setYamlLoading] = useState(false);
    const [yamlSaving, setYamlSaving] = useState(false);
    const [optimizationSelections, setOptimizationSelections] = useState({});
    const [sharedModel, setSharedModel] = useState(returningIntent?.workloads?.[0]?.model || "Qwen/Qwen3-0.6B");
    const [storageVolumeId, setStorageVolumeId] = useState(returningIntent?.runtime?.storage_volume_id || "");
    const modelSource = storageVolumeId ? "auto-cache" : "huggingface";
    const modelPath = "";
    const [storageVolumes, setStorageVolumes] = useState([]);
    const [sharedRuntime] = useState("vllm");
    const [deploymentName, setDeploymentName] = useState(returningIntent?.deployment_name || "evaluation-serving");
    const [deploymentDescription, setDeploymentDescription] = useState(returningIntent?.deployment_description || "");
    const [imageMode] = useState("use-upstream-image");
    const [runtimeImage, setRuntimeImage] = useState(DEFAULT_RUNTIME_IMAGES.xpu);
    const [buildSourceUrl] = useState("");
    const [clusters, setClusters] = useState([]);
    const [selectedClusterId, setSelectedClusterId] = useState(() => returningIntent?.cluster_id || sessionStorage.getItem("prism_cluster_server_id") || "");
    const [connectedCluster, setConnectedCluster] = useState(null);
    const [clusterHardware, setClusterHardware] = useState(null);
    const [cachedRuntimeImages, setCachedRuntimeImages] = useState([]);
    const [clusterLoading, setClusterLoading] = useState(true);
    const [clusterHardwareLoading, setClusterHardwareLoading] = useState(true);
    const [storageLoading, setStorageLoading] = useState(false);
    const [storageError, setStorageError] = useState("");
    const [storageReloadToken, setStorageReloadToken] = useState(0);
    const [cachedModelIds, setCachedModelIds] = useState([]);
    const [cachedModelsLoading, setCachedModelsLoading] = useState(false);
    const [customBenchmark, setCustomBenchmark] = useState(null);
    const [slaTargets, setSlaTargets] = useState({ success_rate_min_percent: 99, ttft_ms: "", ttft_percentile: "p99", tpot_ms: "", tpot_percentile: "p99" });
    const [workloadMode, setWorkloadMode] = useState("profile");
    const [preserveDeployment, setPreserveDeployment] = useState(false);
    const [configurationEditor, setConfigurationEditor] = useState(null);
    const [existingConfigurationsOpen, setExistingConfigurationsOpen] = useState(false);
    const [highlightedArtifactIds, setHighlightedArtifactIds] = useState([]);
    const selectedConfigurationsRef = useRef(null);
    const [targetMode, setTargetMode] = useState(benchmarkOnly ? "existing" : "configurations");
    const [modelServices, setModelServices] = useState([]);
    const [targetId, setTargetId] = useState("");
    const [apiToken, setApiToken] = useState("");
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");

    // A benchmark-only user can never switch to the deploy-a-configuration flow.
    useEffect(() => {
        if (benchmarkOnly && targetMode !== "existing") setTargetMode("existing");
    }, [benchmarkOnly, targetMode]);

    // Align the default runtime image with the selected cluster's hardware once
    // its profile is discovered: a GPU cluster must not stay on the XPU image, or
    // no saved configuration ever matches (``matchesEvaluationSetup`` compares it).
    // Swap only between the known per-vendor defaults so a custom image survives.
    useEffect(() => {
        const variant = acceleratorVariantForHardware(clusterHardware);
        if (!variant) return;
        setRuntimeImage((current) => (
            isDefaultRuntimeImage(current) && current !== DEFAULT_RUNTIME_IMAGES[variant]
                ? DEFAULT_RUNTIME_IMAGES[variant]
                : current
        ));
    }, [clusterHardware]);

    useEffect(() => {
        if (!openMenuId) return undefined;
        const closeMenu = () => setOpenMenuId("");
        const closeOnEscape = (event) => {
            if (event.key === "Escape") closeMenu();
        };
        window.addEventListener("click", closeMenu);
        window.addEventListener("keydown", closeOnEscape);
        return () => {
            window.removeEventListener("click", closeMenu);
            window.removeEventListener("keydown", closeOnEscape);
        };
    }, [openMenuId]);

    useEffect(() => {
        let active = true;
        let catalogError = "";
        setLoading(true);
        setCatalog([]);
        setEditorResources(null);
        const catalogRequest = loadGuideCatalog({ clusterId: selectedClusterId }).catch((error) => {
            catalogError = error.message || "Unable to load cluster guides";
            return { guides: [] };
        });
        Promise.all([catalogRequest, loadConfigurationCapabilities(), loadConfigurationArtifacts(), listModelNames().catch(() => [])])
            .then(([guideCatalog, capabilities, savedArtifacts, services]) => {
                if (!active) return;
                const nextProviders = new Map((capabilities.providers || []).map((provider) => [provider.id, provider]));
                const guides = (guideCatalog.guides || []).filter((guide) => nextProviders.has(guide.id));
                setEditorResources({ catalog: guideCatalog, capabilities });
                setProviders(nextProviders);
                setCatalog(guides);
                setArtifacts(savedArtifacts || []);
                setModelServices(services || []);
                setTargetId(services?.[0]?.id || "");
                setTemplate((current) => guides.some((guide) => guide.id === current) ? current : guides[0]?.id || "");
                if (deployOnly) {
                    const guideId = returningIntent?.workloads?.[0]?.guide || guides[0]?.id || "";
                    setConfigurationEditor({ guideId, mode: "defaults", artifact: null, key: `model-market-${Date.now()}` });
                }
                setError(catalogError);
            })
            .catch((nextError) => active && setError(nextError.message || "Unable to load evaluation inputs"))
            .finally(() => active && setLoading(false));
        return () => { active = false; };
    }, [deployOnly, returningIntent, selectedClusterId]);

    useEffect(() => {
        let active = true;
        setClusterLoading(true);
        loadClusters()
            .then((payload) => {
                if (!active) return;
                const items = Array.isArray(payload?.items) ? payload.items : [];
                setClusters(items);
                setSelectedClusterId((current) => items.some((item) => item.id === current && item.ready) ? current : items.find((item) => item.ready)?.id || "");
            })
            .catch((nextError) => active && setError(nextError.message || "Unable to load clusters"))
            .finally(() => active && setClusterLoading(false));
        return () => { active = false; };
    }, []);

    useEffect(() => {
        if (!selectedClusterId) {
            setConnectedCluster(null);
            setClusterHardware(null);
            setCachedRuntimeImages([]);
            setClusterHardwareLoading(false);
            sessionStorage.removeItem("prism_cluster_session_id");
            sessionStorage.removeItem("prism_cluster_server_id");
            sessionStorage.removeItem("prism_cluster_connection");
            return undefined;
        }
        let active = true;
        // Activate the session on its own: the cluster box and the shared context
        // must not wait for the expensive node/image/hardware overview below.
        setClusterLoading(true);
        selectCluster(selectedClusterId)
            .then((payload) => {
                if (!active) return;
                const nextCluster = {
                    ...payload.cluster,
                    session_id: payload.sessionId,
                    id: payload.cluster.id,
                    name: payload.cluster.name || payload.cluster.id,
                    transport: "managed-kubeconfig",
                };
                setConnectedCluster(nextCluster);
                sessionStorage.setItem("prism_cluster_session_id", nextCluster.session_id);
                sessionStorage.setItem("prism_cluster_server_id", nextCluster.id);
                sessionStorage.setItem("prism_cluster_connection", JSON.stringify({ sessionId: nextCluster.session_id, serverId: nextCluster.id, name: nextCluster.name, transport: nextCluster.transport }));
            })
            .catch((nextError) => active && setError(nextError.message || "Unable to activate cluster"))
            .finally(() => active && setClusterLoading(false));

        // Fill hardware, shared cached images and the runtime-image default in the
        // background; a slow or failed overview must not block the cluster field.
        setClusterHardwareLoading(true);
        loadClusterOverview(selectedClusterId)
            .then((overview) => {
                if (!active) return;
                setClusterHardware(overview?.kubernetes?.hardware || null);
                const workers = (overview?.kubernetes?.nodes || []).filter((node) => node.ready && !node.schedulingDisabled);
                const sharedImages = workers.length
                    ? [...workers.slice(1).reduce(
                        (images, node) => new Set([...images].filter((image) => (node.cachedImages || []).includes(image))),
                        new Set(workers[0].cachedImages || []),
                    )].sort()
                    : [];
                setCachedRuntimeImages(sharedImages);
            })
            .catch(() => active && setClusterHardware(null))
            .finally(() => active && setClusterHardwareLoading(false));
        return () => { active = false; };
    }, [selectedClusterId]);

    useEffect(() => {
        if (!selectedClusterId || !storageVolumeId) {
            setCachedModelIds([]);
            setCachedModelsLoading(false);
            return undefined;
        }
        const controller = new AbortController();
        setCachedModelsLoading(true);
        listModelCacheEntries({ clusterId: selectedClusterId, storageVolumeId, signal: controller.signal })
            .then((entries) => {
                if (controller.signal.aborted) return;
                setCachedModelIds(entries
                    .filter((entry) => entry.status === "ready" && entry.source?.kind === "huggingface")
                    .map((entry) => String(entry.source?.huggingface?.repoId || "").trim())
                    .filter(Boolean));
            })
            .catch((nextError) => {
                if (nextError?.name === "AbortError") return;
                setCachedModelIds([]);
            })
            .finally(() => {
                if (!controller.signal.aborted) setCachedModelsLoading(false);
            });
        return () => controller.abort();
    }, [selectedClusterId, storageVolumeId]);

    useEffect(() => {
        if (!selectedClusterId) {
            setStorageVolumes([]);
            return undefined;
        }
        const controller = new AbortController();
        setStorageLoading(true);
        setStorageError("");
        listStorageVolumes({ clusterId: selectedClusterId, purpose: "model-cache", signal: controller.signal })
            .then((items) => {
                const readyItems = items.filter((item) => item.status === "ready");
                setStorageVolumes(readyItems);
                setStorageVolumeId((current) => readyItems.some((item) => item.id === current) ? current : readyItems.length === 1 ? readyItems[0].id : "");
            })
            .catch((nextError) => {
                if (nextError?.name === "AbortError") return;
                setStorageVolumes([]);
                setStorageError(nextError.message || "Unable to load model-cache storage");
            })
            .finally(() => setStorageLoading(false));
        return () => controller.abort();
    }, [selectedClusterId, storageReloadToken]);

    const provider = providers.get(template);
    const selectedArtifacts = artifactIds.map((id) => artifacts.find((item) => item.artifact_id === id)).filter(Boolean);
    const cluster = connectedCluster || {};
    const trimmedSharedModel = sharedModel.trim();
    // Whether the entered Model ID actually exists (as a ready HF entry) in
    // the selected Model Cache storage -- a deployment sourced from cache
    // (`modelSource === 'auto-cache'`) would otherwise fail to find the model
    // at runtime, so this gates "Continue" below.
    const modelCacheMismatch = Boolean(storageVolumeId) && Boolean(trimmedSharedModel) && !cachedModelsLoading
        && !cachedModelIds.includes(trimmedSharedModel);
    const cacheValidation = storageLoading ? { state: 'loading' }
        : storageError ? { state: 'error', message: storageError }
        : !storageVolumeId ? { state: 'no-volume' }
        : cachedModelsLoading ? { state: 'loading' }
        : modelCacheMismatch ? { state: 'model-mismatch', message: `${trimmedSharedModel} was not found in this Model Cache storage.` }
        : { state: 'ready' };
    const sharedContext = { cluster: clusterLoading ? {} : cluster, clusterHardware, cachedRuntimeImages, model: sharedModel, modelSource, modelPath: modelSource === 'auto-cache' ? storageVolumes.find((item) => item.id === storageVolumeId)?.localDisk?.hostPath || '' : modelPath, storageVolumeId, modelServer: sharedRuntime, deploymentName, imageMode, image: runtimeImage, buildSourceUrl, storageVolume: storageVolumes.find((item) => item.id === storageVolumeId), cacheValidation, replicas: returningIntent?.workloads?.[0]?.replicas, tensorParallelSize: returningIntent?.workloads?.[0]?.tensor_parallel_size };
    const compatibleSavedArtifacts = artifacts.filter((item) => matchesEvaluationSetup(item, sharedContext));
    const modelOptions = [...new Set([...MODELS.map((item) => item.repository), ...cachedModelIds])].filter(Boolean).sort();
    const recommendedBenchmark = benchmarkFor(provider);
    const effectiveBenchmark = customBenchmark || recommendedBenchmark;
    const tests = [benchmarkScenario(effectiveBenchmark, slaTargets)];
    const benchmarkErrors = benchmarkIssues(effectiveBenchmark, slaTargets, configurationContextLimit(selectedArtifacts));
    const selectionFor = artifact => optimizationSelections[artifact.artifact_id] || ['full'];
    const combinationErrors = selectedArtifacts.flatMap(artifact => {
        try { selectedOptimizationPlan(artifact.deployable_configuration.provider_ref, selectionFor(artifact)); return []; }
        catch (error) { return [error.message]; }
    });
    const runCount = targetMode === "existing" ? tests.length : selectedArtifacts.reduce((sum, artifact) => sum + selectionFor(artifact).length, 0) * tests.length;
    const artifactSummaries = selectedArtifacts.map((item) => configurationSummary(item, providers.get(item.deployable_configuration?.provider_ref)));

    useEffect(() => {
        setCustomBenchmark(null);
        setWorkloadMode("profile");
        const nextGuide = catalog.find((item) => item.id === template);
        setName((current) => !current || current === "Evaluation" || catalog.some((item) => current.startsWith(`${item.label} ·`)) ? defaultTaskName(nextGuide) : current);
    }, [template, providers, catalog]);

    useEffect(() => {
        if (step !== 2 || customBenchmark) return;
        const next = structuredClone(recommendedBenchmark || benchmarkFor(provider));
        setCustomBenchmark(next);
        setWorkloadMode(benchmarkWorkloadMode(next));
    }, [step, customBenchmark, recommendedBenchmark, provider]);

    const selectedClusters = new Set(selectedArtifacts.map((item) => artifactCluster(item).session_id).filter(Boolean));
    const setupIssues = [
        (!cluster.session_id || clusterLoading || clusterHardwareLoading || cluster.id !== selectedClusterId) && "Cluster",
        !name.trim() && "Task name",
        !sharedModel.trim() && "Model ID",
        modelCacheMismatch && "Model ID not found in Model Cache storage",
        !deploymentName.trim() && "Deployment Name",
    ].filter(Boolean);
    const canContinue = step === 0
        ? targetMode === "existing"
            ? Boolean(name.trim() && targetId && apiToken.trim())
            : Boolean(name.trim() && deploymentName.trim() && sharedModel.trim() && cluster.session_id && !clusterLoading && !clusterHardwareLoading && cluster.id === selectedClusterId && !modelCacheMismatch)
        : step === 1
            ? targetMode === "existing"
                ? Boolean(targetId && apiToken.trim())
                : Boolean(!configurationEditor && !editorBusy && selectedArtifacts.length && selectedClusters.size === 1 && selectedArtifacts.every((item) => matchesEvaluationSetup(item, sharedContext)))
            : step === 2 ? tests.length > 0 && !benchmarkErrors.length : true;

    const openConfiguration = (guideId = template, mode = "defaults") => {
        storeEvaluationIntent({ operation: deployOnly ? "deploy-only" : "create-evaluation", return_target: deployOnly ? "model-market" : "optimization-evaluate-new", name, deployment_name: deploymentName, deployment_description: deploymentDescription, cluster_id: selectedClusterId, workloads: [{ guide: guideId, model: sharedModel, mount_path: modelPath }], runtime: { model_server: sharedRuntime, model_source: modelSource, mount_path: modelPath, storage_volume_id: storageVolumeId, image_mode: imageMode, image: runtimeImage, build_image_name: runtimeImage, build_source_url: buildSourceUrl }, clone_from_artifact_id: mode === "clone" ? artifactIds.at(-1) : null });
        setConfigurationEditor({ guideId, mode, artifact: mode === "clone" ? selectedArtifacts.at(-1) : null, key: `${guideId}-${Date.now()}` });
    };

    async function deployPublishedConfiguration(publishedArtifacts) {
        setBusy(true);
        setError("");
        try {
            const configurations = (publishedArtifacts || [])
                .map((artifact) => artifact.deployable_configuration)
                .filter(Boolean);
            if (configurations.length !== 1) throw new Error("Advanced deployment requires exactly one configuration.");
            const clusterId = selectedClusterId || cluster.id;
            if (!clusterId) throw new Error("Select a cluster before deploying.");
            if (clusterLoading || clusterHardwareLoading || cluster.id !== clusterId || !cluster.session_id) {
                throw new Error("The selected cluster is still being activated. Wait a moment and deploy again.");
            }
            // The cluster registry owns checkout locations. In particular, do not
            // reconstruct ~/.cache paths in the browser: the API returns the
            // resolved absolute path for the version downloaded for this cluster.
            const latestCluster = await loadCluster(clusterId);
            await startLocalDeployment(configurations, {
                cluster_session_id: cluster.session_id || sessionStorage.getItem("prism_cluster_session_id") || undefined,
                cluster_server_id: clusterId,
                deployment_source: {
                    kind: "model-market-advanced",
                    resolved_repository: latestCluster.llmDRepoPath,
                    ref: latestCluster.llmDRef || undefined,
                },
                deployment_name: deploymentName.trim(),
                description: deploymentDescription.trim(),
                model_market: { deployment_name: deploymentName.trim() },
            });
            clearEvaluationIntent();
            onNavigate("optimization-deployments");
        } catch (nextError) {
            setError(nextError.message || "Deployment could not be created");
            window.requestAnimationFrame(() => window.scrollTo({ top: 0, behavior: "smooth" }));
        } finally {
            setBusy(false);
        }
    }

    const acceptPublishedConfiguration = ({ artifacts: publishedArtifacts, advance = false, optimizationSelection }) => {
        const nextArtifacts = publishedArtifacts || [];
        if (deployOnly) {
            return deployPublishedConfiguration(nextArtifacts);
        }
        if (optimizationSelection) setOptimizationSelections(current => ({...current, ...Object.fromEntries(nextArtifacts.map(item => [item.artifact_id, optimizationSelection]))}));
        // Adopt the accepted configuration's runtime image so it matches exactly
        // (the editor may have used a build-from-source or non-default image).
        const publishedImage = nextArtifacts.find((item) => artifactContent(item).runtime?.image)?.deployable_configuration.content.runtime.image;
        if (publishedImage) setRuntimeImage(publishedImage);
        const nextArtifactIds = nextArtifacts.map((item) => item.artifact_id);
        setArtifacts((current) => [...nextArtifacts, ...current.filter((item) => !nextArtifacts.some((next) => next.artifact_id === item.artifact_id))]);
        setArtifactIds((current) => {
            if (configurationEditor?.mode === 'edit' && current.includes(configurationEditor.artifact?.artifact_id)) {
                const editedId = configurationEditor.artifact?.artifact_id;
                return current.flatMap((id) => id === editedId ? nextArtifacts.map((item) => item.artifact_id) : [id]);
            }
            return [...new Set([...current, ...nextArtifacts.map((item) => item.artifact_id)])];
        });
        setConfigurationSources((current) => ({
            ...current,
            ...Object.fromEntries(nextArtifacts.map((item) => [item.artifact_id, configurationEditor?.mode === 'comparison' ? 'Comparison' : configurationEditor?.mode === 'clone' ? 'Cloned' : configurationEditor?.mode === 'edit' ? 'Customized' : 'Custom'])),
        }));
        if (nextArtifacts.length && (!selectedArtifacts.length || configurationEditor?.artifact?.artifact_id === selectedArtifacts[0]?.artifact_id)) {
            setTemplate(nextArtifacts[0].deployable_configuration?.provider_ref || template);
        }
        setConfigurationNotice(`${nextArtifacts.length} deployable YAML configuration${nextArtifacts.length === 1 ? '' : 's'} saved and selected.`);
        setEditorBusy(false);
        if (advance === 'another') {
            setConfigurationEditor({ guideId: configurationEditor?.guideId || template, mode: 'defaults', artifact: null, key: `next-${Date.now()}` });
        } else {
            setConfigurationEditor(null);
            setStep(1);
            setExistingConfigurationsOpen(false);
            setHighlightedArtifactIds(nextArtifactIds);
            window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
                selectedConfigurationsRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
            }));
            window.setTimeout(() => setHighlightedArtifactIds([]), 2200);
        }
    };

    const leaveSetupFor = (view) => {
        storeEvaluationIntent({ operation: "create-evaluation", return_target: "optimization-evaluate-new", name, deployment_name: deploymentName, deployment_description: deploymentDescription, workloads: [{ guide: template, model: sharedModel, mount_path: modelPath }], runtime: { model_server: sharedRuntime, model_source: modelSource, mount_path: modelPath, storage_volume_id: storageVolumeId, image_mode: imageMode, image: runtimeImage, build_image_name: runtimeImage, build_source_url: buildSourceUrl }, cluster_id: selectedClusterId });
        onNavigate(view);
    };

    const continueWizard = () => {
        if (step === 0) {
            if (targetMode === "existing") {
                setStep(2);
                return;
            }
            // Returning from Setup preserves compatible selections and the open draft.
            const retainedIds = retainCompatibleConfigurations(artifacts, artifactIds, sharedContext);
            setArtifactIds(retainedIds);
            setStep(1);
            if (!configurationEditor && !retainedIds.length) openConfiguration(template);
            return;
        }
        setStep((current) => current + 1);
    };

    const selectSavedConfiguration = (artifactId) => {
        const selectedArtifact = artifacts.find((item) => item.artifact_id === artifactId);
        if (selectedArtifact && !artifactIds.length) setTemplate(selectedArtifact.deployable_configuration?.provider_ref || "");
        const savedImage = selectedArtifact ? artifactContent(selectedArtifact).runtime?.image : "";
        if (savedImage) setRuntimeImage(savedImage);
        setArtifactIds((current) => current.includes(artifactId)
            ? current.filter((id) => id !== artifactId)
            : [...current, artifactId]);
        setConfigurationSources((current) => ({ ...current, [artifactId]: "Saved" }));
    };

    const viewYaml = async (artifact) => {
        setYamlArtifact(artifact);
        setYamlText("");
        setYamlLoading(true);
        try {
            const response = await fetch(`/api/v1/configurations/artifacts/${encodeURIComponent(artifact.artifact_id)}/manifest`);
            if (!response.ok) throw new Error(`Unable to load YAML (${response.status})`);
            setYamlText(await response.text());
        } catch (nextError) {
            setError(nextError.message || "Unable to load configuration YAML");
            setYamlArtifact(null);
        } finally {
            setYamlLoading(false);
        }
    };

    const saveEditedYaml = async () => {
        if (!yamlArtifact || !yamlText.trim()) return;
        setYamlSaving(true);
        try {
            const nextConfiguration = await editedEvaluationConfiguration(yamlArtifact, yamlText);
            const saved = await saveConfiguration(nextConfiguration, `evaluation-edited-${Date.now()}.yaml`);
            const nextArtifact = saved.artifact;
            setArtifacts((current) => [nextArtifact, ...current]);
            setArtifactIds((current) => current.map((id) => id === yamlArtifact.artifact_id ? nextArtifact.artifact_id : id));
            setConfigurationSources((current) => ({ ...current, [nextArtifact.artifact_id]: "YAML edited" }));
            setConfigurationNotice("Edited YAML saved as a new configuration and kept selected.");
            setYamlArtifact(null);
        } catch (nextError) {
            setError(nextError.message || "Unable to save edited YAML");
        } finally {
            setYamlSaving(false);
        }
    };

    const removeArtifact = async (artifact) => {
        if (!await confirmDelete("Delete this deployed configuration YAML? Historical Evaluation evidence is retained.")) return;
        try {
            await deleteConfigurationArtifact(artifact.artifact_id);
            setArtifacts((current) => current.filter((item) => item.artifact_id !== artifact.artifact_id));
            setArtifactIds((current) => current.filter((id) => id !== artifact.artifact_id));
            if (yamlArtifact?.artifact_id === artifact.artifact_id) setYamlArtifact(null);
        } catch (nextError) {
            setError(nextError.message || "Unable to delete configuration");
        }
    };

    const cloneArtifact = async (artifact) => {
        if (!artifact?.deployable_configuration || cloningArtifactId) return;
        setCloningArtifactId(artifact.artifact_id);
        setOpenMenuId("");
        try {
            const source = artifact.deployable_configuration;
            const clonedConfiguration = {
                ...source,
                provenance: {
                    ...(source.provenance || {}),
                    cloned_from_artifact_id: artifact.artifact_id,
                    cloned_at: new Date().toISOString(),
                },
            };
            const providerName = source.provider_ref || source.type || "configuration";
            const saved = await saveConfiguration(clonedConfiguration, `${providerName}-clone-${Date.now()}.yaml`);
            const clone = saved.artifact;
            setOptimizationSelections(current => ({...current, [clone.artifact_id]: [...selectionFor(artifact)]}));
            setArtifacts((current) => [clone, ...current]);
            setArtifactIds((current) => [...current, clone.artifact_id]);
            setConfigurationSources((current) => ({ ...current, [clone.artifact_id]: "Cloned" }));
            setConfigurationNotice("Identical configuration cloned and selected. Use Edit only when changes are needed.");
        } catch (nextError) {
            setError(nextError.message || "Unable to clone configuration");
        } finally {
            setCloningArtifactId("");
        }
    };

    const comparisonGuideOptions = catalog.filter((item) => item.id !== selectedArtifacts[0]?.deployable_configuration?.provider_ref);
    const addComparisonConfiguration = (guideId) => {
        const source = selectedArtifacts[0];
        const targetProvider = providers.get(guideId);
        if (!source || !targetProvider) return;
        setConfigurationEditor({ guideId, mode: 'comparison', artifact: comparisonConfigurationDraft(source, targetProvider), key: `comparison-${guideId}-${Date.now()}` });
    };

    const editConfiguration = (artifact) => {
        storeEvaluationIntent({ operation: "create-evaluation", return_target: "optimization-evaluate-new", name, deployment_name: deploymentName, deployment_description: deploymentDescription, workloads: [{ guide: artifact.deployable_configuration?.provider_ref || template, model: sharedModel, mount_path: modelPath }], runtime: { model_server: sharedRuntime, model_source: modelSource, mount_path: modelPath, storage_volume_id: storageVolumeId, image_mode: imageMode, image: runtimeImage, build_image_name: runtimeImage, build_source_url: buildSourceUrl } });
        setConfigurationEditor({ guideId: artifact.deployable_configuration?.provider_ref || template, mode: "edit", artifact, key: `${artifact.artifact_id}-${Date.now()}` });
        setOpenMenuId("");
    };

    const selectWorkloadMode = (mode, baseBenchmark = null) => {
        setWorkloadMode(mode);
        setCustomBenchmark((previous) => {
            const current = baseBenchmark || previous || recommendedBenchmark;
            return ({
            ...current,
            workload: mode === "matrix" || mode === "shared-prefix" ? "sanity_random.yaml" : current.workload,
            matrix: mode === "matrix" ? current?.matrix?.length ? current.matrix : baseBenchmark?.matrix?.length ? baseBenchmark.matrix : [{ isl: 1024, osl: 128 }] : [],
            concurrency_stages: mode === "matrix" ? current?.concurrency_stages?.length ? current.concurrency_stages : baseBenchmark?.concurrency_stages?.length ? baseBenchmark.concurrency_stages : [{ concurrency: 1, num_requests: 32 }] : [],
            shared_prefix: mode === "shared-prefix" ? current?.shared_prefix || baseBenchmark?.shared_prefix || {
                num_groups: 60, num_prompts_per_group: 5, system_prompt_len: 3000,
                question_len: 256, output_len: 256, enable_multi_turn_chat: false,
                stages: [{ rate: 1, duration: 60 }],
            } : null,
            workload_yaml: mode === "yaml" ? current?.workload_yaml || "load:\n  type: constant\n  stages:\n    - rate: 1\n      duration: 30\napi:\n  type: completion\n  streaming: true\ndata:\n  type: random\n  input_distribution: {min: 1024, max: 1024, mean: 1024, std_dev: 0}\n  output_distribution: {min: 128, max: 128, mean: 128, std_dev: 0}\n" : null,
        }); });
    };

    const create = async () => {
        if (busy) return;
        if (benchmarkErrors.length) { setError(benchmarkErrors.join(" ")); return; }
        setBusy(true);
        setError("");
        try {
            if (targetMode === "existing") {
                const resolvedTargetId = resolveEvaluationTarget({ targetId });
                const createdRuns = await Promise.all(tests.map((test) => createBenchmarkRun({
                    ...effectiveBenchmark,
                    ...test.benchmark,
                    deployment_execution_id: null,
                    model_service_group_id: resolvedTargetId,
                    wait_timeout_seconds: effectiveBenchmark.wait_timeout_seconds,
                    harness_memory_gib: Number(test.benchmark?.harness_memory_gib ?? effectiveBenchmark.harness_memory_gib ?? 32),
                    sla_targets: benchmarkScenario(effectiveBenchmark, slaTargets).sla_targets,
                    harness: test.benchmark?.harness || effectiveBenchmark.harness,
                    workload: test.benchmark?.workload || effectiveBenchmark.workload,
                    parallelism: Number(test.benchmark?.parallelism ?? effectiveBenchmark.parallelism ?? 1),
                    matrix: test.benchmark?.matrix || effectiveBenchmark.matrix || [],
                    concurrency_stages: test.benchmark?.concurrency_stages || effectiveBenchmark.concurrency_stages || [],
                    warmup_requests: Number(test.benchmark?.warmup_requests ?? effectiveBenchmark.warmup_requests ?? 2),
                    shared_prefix: test.benchmark?.shared_prefix || effectiveBenchmark?.shared_prefix || undefined,
                    workload_yaml: test.benchmark?.workload_yaml || effectiveBenchmark?.workload_yaml || null,
                    api_key: apiToken.trim() || null,
                })));
                window.dispatchEvent(new CustomEvent("prism:evaluation-created", { detail: { benchmarkRuns: createdRuns } }));
                onNavigate("optimization-evaluate");
                return;
            }
            if (!selectedArtifacts.length || selectedClusters.size !== 1) throw new Error("Select one or more setups bound to the same active cluster session.");
            if (combinationErrors.length) throw new Error(combinationErrors[0]);
            const evaluationArtifacts = selectedArtifacts;
            const suffix = timestampSuffix();
            const namedArtifacts = evaluationArtifacts.map((artifact, index) => ({
                ...artifact,
                deployable_configuration: {
                    ...artifact.deployable_configuration,
                    provenance: {
                        ...(artifact.deployable_configuration?.provenance || {}),
                        deployment_name: deploymentServiceName(deploymentName, artifact, index, suffix),
                    },
                },
            }));
            const createdWorkflow = await createEvaluation({
                name: name.trim(),
                cluster_session_id: cluster.session_id,
                runtime: {
                    http_proxy: "http://proxy.ims.intel.com:911",
                    https_proxy: "http://proxy.ims.intel.com:911",
                    no_proxy: "intel.com,.intel.com,localhost,127.0.0.1",
                },
                compare_configurations: evaluationArtifacts.length > 1,
                benchmark_plans: namedArtifacts.map((selectedArtifact, index) => ({
                    id: `setup-${index + 1}`,
                    configuration_artifact_id: selectedArtifact.artifact_id,
                    deployment_name: selectedArtifact.deployable_configuration.provenance.deployment_name,
                    deployment_description: deploymentDescription.trim(),
                    benchmark: effectiveBenchmark,
                    ...selectedOptimizationPlan(selectedArtifact.deployable_configuration.provider_ref, selectionFor(selectedArtifact)),
                    preserve_deployment: preserveDeployment,
                    scenarios: tests,
                })),
            });
            window.dispatchEvent(new CustomEvent("prism:evaluation-created", { detail: { workflow: createdWorkflow } }));
            onNavigate("optimization-evaluate");
        } catch (nextError) {
            setError(nextError.message || "Evaluation could not be created");
            window.requestAnimationFrame(() => window.scrollTo({ top: 0, behavior: "smooth" }));
        } finally {
            setBusy(false);
        }
    };

    const leaveDeployOnly = () => {
        sessionStorage.setItem("prism_model_market_return_draft", JSON.stringify({ model: sharedModel }));
        sessionStorage.setItem("prism_model_market_return_draft_values", JSON.stringify({
            deployName: deploymentName,
            description: deploymentDescription,
            cluster: selectedClusterId,
            replicas: returningIntent?.workloads?.[0]?.replicas,
            tp: returningIntent?.workloads?.[0]?.tensor_parallel_size,
            storageVolumeId,
            mode: "advanced",
        }));
        clearEvaluationIntent();
        onNavigate("model-market");
    };

    return <section className="compact-evaluation-wizard relative isolate mx-auto w-full max-w-[1500px] space-y-5 overflow-hidden px-6 py-6 text-slate-100 lg:px-8">
        <div aria-hidden="true" className="pointer-events-none absolute -right-32 top-24 -z-10 h-80 w-80 rounded-full bg-cyan-500/[0.07] blur-3xl" />
        <div aria-hidden="true" className="pointer-events-none absolute -left-40 top-96 -z-10 h-96 w-96 rounded-full bg-blue-600/[0.05] blur-3xl" />
        <header className="flex items-center justify-between rounded-2xl border border-slate-800/70 bg-slate-950/35 px-5 py-4 shadow-lg shadow-black/10 backdrop-blur-sm">
            <div>
                <button disabled={editorBusy || busy} onClick={() => deployOnly ? leaveDeployOnly() : onNavigate("optimization-evaluate")} className="mb-2 inline-flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 transition hover:text-cyan-300"><ArrowLeft className="h-3.5 w-3.5" />{deployOnly ? "Back to model card" : "Evaluation tasks"}</button>
                <h1 className="text-2xl font-bold tracking-tight">{deployOnly ? "Configure deployment" : "New Benchmark Task"}</h1>
                <p className="mt-1 text-xs leading-5 text-slate-400">{deployOnly ? `Advanced-edit the deployment configuration for ${sharedModel}, then deploy it directly.` : "Design the complete experiment before deployment starts."}</p>
            </div>
        </header>
        {error && <div role="alert" className="flex min-w-0 items-start gap-3 rounded-xl border border-rose-400/30 bg-rose-500/10 p-4 text-sm text-rose-200"><AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" /><div className="min-w-0"><p className="font-semibold">{step === 3 ? 'Evaluation could not start' : 'Unable to continue'}</p><p className="mt-1 break-words text-xs leading-6">{error}</p></div></div>}
        {!deployOnly && <ol className="grid grid-cols-4 gap-2 rounded-2xl border border-slate-800/60 bg-slate-950/30 p-2 shadow-inner" aria-label="Task creation progress">{STEPS.map((label, index) => <li key={label}><button disabled={editorBusy || index > step || (targetMode === "existing" && index === 1)} onClick={() => setStep(index)} className={`flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left transition disabled:cursor-not-allowed ${index === step ? "bg-cyan-500/10 shadow-sm ring-1 ring-cyan-400/25" : index < step ? "hover:bg-slate-800/50" : "opacity-55"}`}><span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[9px] font-bold ${index < step ? "bg-emerald-400/15 text-emerald-300" : index === step ? "bg-cyan-400 text-slate-950 shadow-[0_0_14px_rgba(34,211,238,.35)]" : "bg-slate-800 text-slate-500"}`}>{index < step ? <Check className="h-3.5 w-3.5" /> : index + 1}</span><span className={`hidden truncate text-[9px] font-bold uppercase tracking-wide sm:block lg:text-[10px] ${index === step ? "text-cyan-200" : index < step ? "text-slate-300" : "text-slate-500"}`}>{label}</span></button></li>)}</ol>}
        {loading ? <div className="flex min-h-80 items-center justify-center"><RefreshCw className="h-6 w-6 animate-spin text-cyan-400" /></div> : <div>
            <main className="rounded-3xl border border-slate-800/70 bg-[#080d17]/95 p-5 shadow-[0_24px_80px_rgba(2,6,23,.32)] backdrop-blur-sm lg:p-6">
                {!deployOnly && step === 0 && <div className="space-y-4">
                    {benchmarkOnly && <p className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] leading-5 text-amber-200">Your role can only benchmark an existing deployment. Designing and deploying a new one from configurations needs the deployment-create permission.</p>}
                    <div className="grid items-end gap-3 lg:grid-cols-[1fr_320px]"><div><h2 className="text-lg font-bold">Evaluation Setup</h2><p className="mt-1 text-[11px] text-slate-500">Choose a target and provide only the shared context it needs.</p></div><label className="text-[11px] text-slate-400">Task name<input className={`${inputClass} h-9`} value={name} onChange={(event) => setName(event.target.value)} /></label></div>
                    <section className="relative overflow-hidden rounded-2xl border border-cyan-500/15 bg-gradient-to-br from-slate-900/80 via-slate-950/70 to-cyan-950/15 p-4 shadow-lg shadow-black/10"><div aria-hidden="true" className="absolute -right-16 -top-20 h-40 w-40 rounded-full bg-cyan-400/[0.06] blur-3xl" /><div className="relative mb-3 flex items-center justify-between"><div><p className="text-[9px] font-bold uppercase tracking-[0.18em] text-cyan-400">Target</p><h3 className="mt-1 text-sm font-semibold text-slate-100">How should this evaluation run?</h3></div><span className="rounded-full border border-slate-700/80 bg-slate-950/60 px-2.5 py-1 text-[9px] text-slate-500">Step 1</span></div><div className="relative grid gap-3 md:grid-cols-2"><button type="button" onClick={() => setTargetMode("existing")} className={`group rounded-xl border px-4 py-3 text-left transition hover:-translate-y-0.5 ${targetMode === "existing" ? "border-cyan-400/70 bg-cyan-500/10" : "border-slate-800 bg-slate-950/30 hover:border-slate-700"}`}><span className="text-xs font-semibold">Use existing endpoint</span><span className="mt-0.5 block text-[9px] text-slate-500">Skip deployment design and continue to Benchmark.</span></button>{!benchmarkOnly && <button type="button" onClick={() => setTargetMode("configurations")} className={`group rounded-xl border px-4 py-3 text-left transition hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-40 ${targetMode === "configurations" ? "border-cyan-400/70 bg-cyan-500/10" : "border-slate-800 bg-slate-950/30 hover:border-slate-700"}`}><span className="text-xs font-semibold">Design configurations</span><span className="mt-0.5 block text-[9px] text-slate-500">Set shared deployment context, then choose configurations.</span></button>}</div></section>
                    {targetMode === "existing" ? <section className="rounded-xl border border-slate-800 bg-slate-950/35 p-3"><label className="block text-[11px] text-slate-400">Model service<select className={`${inputClass} h-9`} value={targetId} onChange={(event) => setTargetId(event.target.value)}><option value="">Select a published model service</option>{modelServices.map((item) => <option key={item.id} value={item.id}>{item.name}{item.baseModel && item.baseModel !== item.name ? ` · ${item.baseModel}` : ""}{item.clusterName ? ` (${item.clusterName})` : ""}</option>)}</select>{!modelServices.length && <span className="mt-1 block text-[9px] text-amber-300">No model service is published and healthy yet. Publish one from the Model Service page first.</span>}</label><label className="mt-2 block text-[11px] text-slate-400">Model access token<input type="password" autoComplete="off" className={`${inputClass} h-9`} placeholder="lens-mk-…" value={apiToken} onChange={(event) => setApiToken(event.target.value)} /><span className="mt-1 block text-[9px] text-slate-500">Required: the harness authenticates through the cluster&apos;s shared Gateway. Used for this run only; never stored.</span></label></section> : <>
                    <div className="grid grid-cols-2 gap-3">
                        <section className="rounded-xl border border-slate-800 bg-gradient-to-br from-slate-950/70 to-cyan-950/10 p-3">
                            <div className="flex items-center justify-between"><SectionLabel>Environment</SectionLabel><div className="flex items-center gap-2"><span className={`text-[9px] font-semibold ${cluster.session_id ? "text-emerald-300" : "text-amber-300"}`}>{clusterLoading ? "Checking…" : cluster.session_id ? "Connected ✓" : "Required"}</span><button type="button" onClick={() => leaveSetupFor("clusters")} title="Manage clusters" aria-label="Manage clusters" className="inline-flex h-7 w-7 items-center justify-center rounded-lg border border-slate-700 text-slate-400 transition hover:border-cyan-500/50 hover:bg-cyan-500/10 hover:text-cyan-200"><ExternalLink className="h-3.5 w-3.5" /></button></div></div>
                            <label className="mt-2 block text-[11px] text-slate-400">Cluster<select className={`${inputClass} h-9`} value={selectedClusterId} disabled={clusterLoading && !clusters.length} onChange={(event) => setSelectedClusterId(event.target.value)}><option value="">Select a ready cluster</option>{clusters.map((item) => <option key={item.id} value={item.id} disabled={!item.ready}>{item.name} ({item.id}){item.ready ? "" : " — unavailable"}</option>)}</select></label>
                            <div className="mt-2 grid grid-cols-3 divide-x divide-slate-800 rounded-lg bg-slate-950/55 py-1.5 text-center"><div><p className="text-[8px] text-slate-500">Total</p><p className="text-xs font-semibold">{clusterHardware?.gpuCount ?? clusterHardware?.availableGpuCount ?? (clusterHardwareLoading ? "…" : "—")}</p></div><div><p className="text-[8px] text-slate-500">Available</p><p className="text-xs font-semibold text-emerald-300">{clusterHardware?.usableGpuCount ?? clusterHardware?.availableGpuCount ?? (clusterHardwareLoading ? "…" : "—")}</p></div><div><p className="text-[8px] text-slate-500">Metrics</p><p className="text-xs font-semibold text-emerald-300">Ready</p></div></div>
                        </section>
                        <section className="rounded-xl border border-slate-800 bg-gradient-to-br from-slate-950/70 to-violet-950/10 p-3">
                            <div className="flex items-center justify-between"><SectionLabel>Model</SectionLabel><span className="text-[9px] text-slate-500">Model Market + Model Cache</span></div>
                            <label className="mt-3 block text-xs text-slate-400">Model ID<input list="evaluation-model-options" className={inputClass} value={sharedModel} placeholder="Organization/model-name" onChange={(event) => setSharedModel(event.target.value)} /><datalist id="evaluation-model-options">{modelOptions.map((item) => <option key={item} value={item} />)}</datalist></label>
                            {storageVolumeId && trimmedSharedModel && (
                                cachedModelsLoading
                                    ? <p className="mt-1 text-[10px] text-slate-500">Checking Model Cache storage…</p>
                                    : modelCacheMismatch
                                        ? <p className="mt-1 text-[10px] text-rose-300">Not found in this Model Cache storage — pick a cached model or clear the storage selection.</p>
                                        : <p className="mt-1 text-[10px] text-emerald-300">Found in Model Cache storage ✓</p>
                            )}
                            <div className="mt-3"><div className="flex items-center justify-between"><span className="text-xs text-slate-400">Model Cache Storage</span><div className="flex items-center gap-3"><button type="button" onClick={() => setStorageReloadToken((current) => current + 1)} disabled={!selectedClusterId || storageLoading} className="inline-flex items-center gap-1 text-[10px] text-slate-400 hover:text-cyan-200 disabled:opacity-40"><RefreshCw className={`h-3 w-3 ${storageLoading ? "animate-spin" : ""}`} />Refresh</button><button type="button" onClick={() => leaveSetupFor("model-cache")} className="inline-flex items-center gap-1 text-[10px] text-cyan-300 hover:text-cyan-200">Open Model Cache<ExternalLink className="h-3 w-3" /></button></div></div><StorageVolumeSelect clearable className="mt-1" value={storageVolumeId} onChange={setStorageVolumeId} volumes={storageVolumes} loading={storageLoading} disabled={!selectedClusterId} placeholder="Optional model-cache storage" emptyLabel="No ready Model Cache storage" optionDetail={(volume) => `${volume.kind} · ${volume.capacity}`} />{storageError && <p className="mt-1 text-[10px] text-rose-300">{storageError}</p>}</div>
                            <label className="mt-3 block border-t border-slate-800 pt-3 text-xs text-slate-400">Deployment Name<input className={inputClass} value={deploymentName} placeholder="evaluation-serving" onChange={(event) => setDeploymentName(event.target.value)} /></label>
                            <label className="mt-3 block text-xs text-slate-400">Deployment description <span className="text-[9px] text-slate-600">Optional</span><textarea className={`${inputClass} h-auto min-h-16 py-2`} value={deploymentDescription} maxLength={1000} placeholder="Purpose, owner, or experiment notes" onChange={(event) => setDeploymentDescription(event.target.value)} /></label>
                        </section>
                    </div>
                    </>}
                </div>}

                {configurationEditor && <div hidden={step !== 1 || targetMode !== 'configurations'}>
                    <EvaluationConfigurationEditor key={configurationEditor.key} sharedContext={sharedContext} editorResources={editorResources} onBusyChange={setEditorBusy} initialGuide={configurationEditor.guideId} initialArtifact={configurationEditor.artifact} initialOptimizationSelection={configurationEditor.artifact ? optimizationSelections[configurationEditor.artifact.artifact_id] : undefined} busy={editorBusy || busy} onEditSetup={deployOnly ? undefined : () => setStep(0)} onNavigate={onNavigate} onCancel={deployOnly ? undefined : () => setConfigurationEditor(null)} onUseSavedConfigurations={!deployOnly && !selectedArtifacts.length && compatibleSavedArtifacts.length ? () => { setConfigurationEditor(null); setExistingConfigurationsOpen(true); } : undefined} onPublished={acceptPublishedConfiguration} singleConfiguration={deployOnly} deployOnPublish={deployOnly} />
                </div>}
                {!deployOnly && step === 1 && !configurationEditor && <div className="min-w-0 space-y-5">
                    <div className="flex flex-wrap items-end justify-between gap-4 border-b border-slate-800/70 pb-5">
                        <div className="flex items-start gap-3">
                            <span className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-cyan-400/25 bg-cyan-400/10 text-cyan-300 shadow-[0_0_24px_rgba(34,211,238,.08)]"><Layers3 className="h-5 w-5" /></span>
                            <div><p className="text-[9px] font-bold uppercase tracking-[0.2em] text-cyan-400">Step 2 · Deployment design</p><h2 className="mt-1 text-xl font-bold tracking-tight">Configurations for this evaluation</h2><p className="mt-1 max-w-2xl text-[11px] leading-5 text-slate-500">Review the configurations for this evaluation, add comparison configurations, then continue to Benchmark.</p></div>
                        </div>
                        <span className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-[10px] font-semibold ${selectedArtifacts.length ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-300" : "border-slate-700 bg-slate-900/70 text-slate-400"}`}><span className={`h-1.5 w-1.5 rounded-full ${selectedArtifacts.length ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,.8)]" : "bg-slate-600"}`} />{selectedArtifacts.length ? `${selectedArtifacts.length} selected` : "None selected"}</span>
                    </div>
                    {configurationNotice && <p role="status" className="rounded-lg border border-cyan-500/20 bg-cyan-500/5 px-3 py-2 text-xs text-cyan-200">{configurationNotice}</p>}
                        <section className="relative overflow-hidden rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/80 via-slate-950/80 to-cyan-950/20 p-4 shadow-lg shadow-black/10">
                            <div aria-hidden="true" className="absolute -right-16 -top-20 h-44 w-44 rounded-full bg-cyan-400/[0.06] blur-3xl" />
                            <div className="relative flex items-center justify-between gap-4"><div><p className="text-[9px] font-bold uppercase tracking-[0.18em] text-slate-500">Shared deployment context</p><h3 className="mt-1.5 max-w-3xl truncate text-sm font-semibold text-slate-100" title={sharedModel}>{sharedModel}</h3></div>{!deployOnly && <button type="button" onClick={() => setStep(0)} className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-slate-700/80 bg-slate-950/60 px-3 text-[10px] font-semibold text-cyan-200 transition hover:border-cyan-500/50 hover:bg-cyan-500/10"><Pencil className="h-3 w-3" />Edit setup</button>}</div>
                            <div className="relative mt-4 grid gap-2 sm:grid-cols-3">
                                <div className="flex min-w-0 items-center gap-3 rounded-xl border border-slate-800/80 bg-slate-950/55 p-3"><span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-emerald-400/10 text-emerald-300"><Server className="h-4 w-4" /></span><div className="min-w-0"><p className="text-[8px] font-semibold uppercase tracking-wider text-slate-500">Cluster</p><p className="mt-0.5 truncate text-xs font-semibold">{cluster.name || cluster.id || "—"}</p></div></div>
                                <div className="flex min-w-0 items-center gap-3 rounded-xl border border-slate-800/80 bg-slate-950/55 p-3"><span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-violet-400/10 text-violet-300"><Cpu className="h-4 w-4" /></span><div className="min-w-0"><p className="text-[8px] font-semibold uppercase tracking-wider text-slate-500">Runtime</p><p className="mt-0.5 truncate text-xs font-semibold">{sharedRuntime || "—"}</p></div></div>
                                <div className="flex min-w-0 items-center gap-3 rounded-xl border border-slate-800/80 bg-slate-950/55 p-3"><span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-blue-400/10 text-blue-300"><Boxes className="h-4 w-4" /></span><div className="min-w-0"><p className="text-[8px] font-semibold uppercase tracking-wider text-slate-500">Runtime image</p><p className="mt-0.5 truncate font-mono text-[9px] text-slate-300" title={runtimeImage}>{runtimeImage || "—"}</p></div></div>
                            </div>
                        </section>
                        <div ref={selectedConfigurationsRef} className="grid scroll-mt-24 grid-cols-1 items-stretch gap-3 lg:grid-cols-2 xl:grid-cols-3">
                            {selectedArtifacts.map((item, index) => { const facts = configurationFacts(item, providers.get(item.deployable_configuration?.provider_ref)); const content = artifactContent(item); const decode = content.decode || content.serving || {}; const highlighted = highlightedArtifactIds.includes(item.artifact_id); return <article key={item.artifact_id} className={`relative flex min-h-[230px] flex-col overflow-hidden rounded-2xl border p-4 shadow-lg shadow-black/10 transition-all duration-300 ${highlighted ? "animate-pulse border-cyan-300 bg-cyan-300/15 ring-2 ring-cyan-300/40" : index === 0 ? "border-cyan-500/30 bg-gradient-to-br from-cyan-950/25 via-slate-950/70 to-slate-950/90 hover:-translate-y-0.5 hover:border-cyan-400/50" : "border-violet-500/25 bg-gradient-to-br from-violet-950/20 via-slate-950/70 to-slate-950/90 hover:-translate-y-0.5 hover:border-violet-400/45"}`}>
                                <div aria-hidden="true" className={`absolute inset-x-0 top-0 h-0.5 ${index === 0 ? "bg-gradient-to-r from-cyan-400 via-blue-400 to-transparent" : "bg-gradient-to-r from-violet-400 via-fuchsia-400 to-transparent"}`} />
                                <div className="flex flex-wrap items-start justify-between gap-3">
                                    <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className={`flex h-7 w-7 items-center justify-center rounded-lg border text-[10px] font-black ${index === 0 ? "border-cyan-400/30 bg-cyan-400/10 text-cyan-300" : "border-violet-400/30 bg-violet-400/10 text-violet-300"}`}>{String.fromCharCode(65 + index)}</span><div><div className="flex items-center gap-1.5"><h3 className="text-xs font-semibold text-slate-100">{facts.guide}</h3><Badge tone={index === 0 ? 'info' : 'violet'} size="xs">{index === 0 ? 'Primary' : 'Comparison'}</Badge></div><p title={configurationSources[item.artifact_id] || "Saved"} className="mt-0.5 text-[9px] text-slate-500">Saved configuration</p></div></div><p className="mt-3 truncate text-[10px] text-slate-400" title={`${facts.candidate} · ${facts.model}`}>{facts.candidate} · {facts.model}</p></div>
                                    <div className="flex items-center gap-1"><button type="button" onClick={() => editConfiguration(item)} className="inline-flex h-8 items-center gap-1 rounded-lg border border-cyan-500/25 bg-cyan-500/[0.07] px-2.5 text-[10px] font-semibold text-cyan-200 transition hover:border-cyan-400/50 hover:bg-cyan-500/15"><Pencil className="h-3 w-3" />Edit</button><button type="button" onClick={() => viewYaml(item)} className="h-8 rounded-lg border border-slate-700/80 px-2.5 text-[10px] text-slate-400 transition hover:border-slate-600 hover:bg-slate-800/70 hover:text-slate-200">YAML</button><button type="button" aria-label={`Configuration ${String.fromCharCode(65 + index)} actions`} onClick={(event) => { event.stopPropagation(); setOpenMenuId((current) => current === item.artifact_id ? '' : item.artifact_id); }} className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 transition hover:bg-slate-800 hover:text-slate-200"><Ellipsis className="h-4 w-4" /></button></div>
                                </div>
                                {openMenuId === item.artifact_id && <div onClick={(event) => event.stopPropagation()} className="absolute right-3 top-12 z-20 w-36 rounded-lg border border-slate-700 bg-slate-950 p-1 text-xs shadow-xl"><button disabled={Boolean(cloningArtifactId)} onClick={() => cloneArtifact(item)} className="block w-full rounded px-2 py-1.5 text-left hover:bg-slate-800 disabled:opacity-40">{cloningArtifactId === item.artifact_id ? "Cloning…" : "Clone identical"}</button>{index > 0 && <button onClick={() => { setArtifactIds((current) => [item.artifact_id, ...current.filter((id) => id !== item.artifact_id)]); setTemplate(item.deployable_configuration?.provider_ref || template); setOpenMenuId(''); }} className="block w-full rounded px-2 py-1.5 text-left hover:bg-slate-800">Use as Primary</button>}<button onClick={() => { setArtifactIds((current) => current.filter((id) => id !== item.artifact_id)); setOpenMenuId(''); }} className="block w-full rounded px-2 py-1.5 text-left text-rose-300 hover:bg-rose-500/10">Remove from task</button></div>}
                                <dl className="mt-auto grid grid-cols-2 gap-2 pt-4 text-[10px]">{[
                                    ['Topology', facts.topology], ['Accelerators', facts.gpu || '—'],
                                    ['Max context', decode.maxModelLen || decode.max_model_len || content.model?.maxModelLen ? Number(decode.maxModelLen || decode.max_model_len || content.model?.maxModelLen).toLocaleString() : '—'],
                                    ['Max sequences', decode.maxNumSeqs || decode.max_num_seqs || content.customParameters?.find((parameter) => parameter.name === 'max-num-seqs')?.value || '—'],
                                    ['Block size', facts.blockSize || 'Guide default'],
                                ].map(([label, value]) => <div key={label} className="rounded-lg border border-slate-800/70 bg-black/15 px-2.5 py-2"><dt className="text-[8px] uppercase tracking-wide text-slate-600">{label}</dt><dd className="mt-0.5 truncate font-semibold text-slate-200" title={String(value)}>{value}</dd></div>)}</dl>
                                <OptimizationSelectionSummary guide={item.deployable_configuration.provider_ref} selected={selectionFor(item)} />
                            </article>; })}
                            <button type="button" onClick={() => openConfiguration(template)} className={`group relative flex items-center gap-4 overflow-hidden rounded-2xl border text-left font-semibold transition-all duration-200 ${selectedArtifacts.length ? "min-h-[110px] self-start border-dashed border-slate-700 bg-slate-950/25 px-5 py-4 text-xs text-slate-300 hover:-translate-y-0.5 hover:border-cyan-500/50 hover:bg-cyan-500/[0.05] hover:text-cyan-200" : "col-span-full min-h-32 w-full border-cyan-400/60 bg-gradient-to-r from-cyan-500/25 via-blue-500/15 to-violet-500/15 px-6 py-5 text-sm text-cyan-50 shadow-[0_16px_40px_rgba(8,145,178,.12)] ring-1 ring-cyan-300/15 hover:-translate-y-0.5 hover:border-cyan-300 hover:from-cyan-500/35 hover:shadow-[0_20px_50px_rgba(8,145,178,.2)]"}`}>
                                <span aria-hidden="true" className="absolute -right-12 -top-20 h-48 w-48 rounded-full bg-cyan-300/10 blur-2xl transition group-hover:bg-cyan-300/15" />
                                <span className={`flex shrink-0 items-center justify-center rounded-xl border transition ${selectedArtifacts.length ? "h-8 w-8 border-slate-700 bg-slate-900 text-cyan-300 group-hover:border-cyan-500/40" : "h-11 w-11 border-cyan-300/50 bg-cyan-400 text-slate-950 shadow-md shadow-cyan-950/40 group-hover:scale-105"}`}><Plus className={selectedArtifacts.length ? "h-4 w-4" : "h-5 w-5"} /></span>
                                <span className="relative min-w-0 flex-1"><span className="block">{selectedArtifacts.length ? 'Add another configuration' : 'Create configuration'}</span><span className={`mt-1 block font-normal leading-5 ${selectedArtifacts.length ? "text-[9px] text-slate-500" : "text-xs text-cyan-100/70"}`}>{selectedArtifacts.length ? "Build another setup from Guide defaults" : "Recommended · Start with tuned Guide defaults, then adjust the deployment for your workload."}</span></span>
                                {!selectedArtifacts.length && <ChevronRight className="h-5 w-5 shrink-0 text-cyan-300 transition group-hover:translate-x-1" />}
                            </button>
                        </div>
                        <details open={existingConfigurationsOpen} onToggle={(event) => setExistingConfigurationsOpen(event.currentTarget.open)} className="configuration-saved-panel group overflow-hidden rounded-2xl border border-slate-800/80 bg-slate-950/30 shadow-lg shadow-black/5"><summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-4 py-3.5 transition hover:bg-slate-900/50"><div className="flex items-center gap-3"><span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-slate-700/70 bg-slate-900 text-slate-400 transition group-hover:text-cyan-300"><Code2 className="h-4 w-4" /></span><div><div className="flex flex-wrap items-center gap-2"><h3 className="text-xs font-semibold text-slate-200">Add from saved configurations</h3><span className="rounded-full border border-slate-700/70 bg-slate-900/80 px-2 py-0.5 text-[8px] font-semibold text-slate-500">{compatibleSavedArtifacts.length} available</span></div><p className="mt-1 text-[9px] text-slate-500">Optional · Reuse a compatible setup from this cluster.</p></div></div><ChevronDown className="h-4 w-4 shrink-0 text-slate-500 transition group-open:rotate-180 group-hover:text-cyan-300" /></summary><div className="h-64 space-y-2 overflow-y-auto overscroll-contain border-t border-slate-800/70 bg-black/10 p-3">{compatibleSavedArtifacts.length ? compatibleSavedArtifacts.map((item) => { const facts = configurationFacts(item, providers.get(item.deployable_configuration?.provider_ref)); const selected = artifactIds.includes(item.artifact_id); return <div key={item.artifact_id} className={`flex items-center gap-2 rounded-xl border p-2.5 transition ${selected ? "border-cyan-400/50 bg-cyan-400/[0.08]" : "border-slate-800 bg-slate-950/35 hover:border-slate-700 hover:bg-slate-900/50"}`}><button type="button" onClick={() => selectSavedConfiguration(item.artifact_id)} className="flex min-w-0 flex-1 items-center gap-3 text-left disabled:opacity-40"><span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-md border ${selected ? "border-cyan-400 bg-cyan-400 text-slate-950" : "border-slate-600"}`}>{selected && <Check className="h-3.5 w-3.5" />}</span><span className="min-w-0 flex-1"><span className="block truncate font-mono text-[10px] font-semibold text-cyan-200">{item.configuration_file?.file_name || item.artifact_id}</span><span className="mt-1 block text-[9px] text-slate-500">{facts.guide} · {facts.topology} · {facts.model} · {artifactCluster(item).name || "Unknown cluster"}</span></span></button><button type="button" onClick={() => editConfiguration(item)} className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-slate-700 px-2 py-1.5 text-[9px] text-slate-400 hover:text-cyan-200"><Pencil className="h-3 w-3" />Edit</button><button type="button" onClick={() => viewYaml(item)} className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-slate-700 px-2 py-1.5 text-[9px] text-slate-400 hover:text-cyan-200"><Code2 className="h-3 w-3" />Edit YAML</button><button type="button" title="Delete deployed configuration" aria-label={`Delete ${item.configuration_file?.file_name || item.artifact_id}`} onClick={() => removeArtifact(item)} className="shrink-0 rounded-lg border border-red-500/25 p-1.5 text-red-300 hover:bg-red-500/10"><Trash2 className="h-3 w-3" /></button></div>; }) : <p className="flex h-full items-center justify-center rounded-lg border border-dashed border-slate-700 text-center text-xs text-slate-500">No saved configurations.</p>}</div></details>
                        {selectedArtifacts.length > 0 && <section className="rounded-xl border border-violet-500/25 bg-violet-500/[0.04] p-4">
                            <h3 className="flex items-center gap-2 text-xs font-bold"><GitCompareArrows className="h-4 w-4 text-violet-300" />Comparison configurations</h3>
                            <p className="mt-1 text-[10px] text-slate-400">Create and validate another Guide using the first configuration's runtime controls and shared setup. Saved comparisons appear in the selected configurations above. Prefill-only overrides are omitted for single-pool Guides.</p>
                            <div className="mt-3 flex flex-wrap gap-2">{comparisonGuideOptions.map(guide => <button type="button" key={guide.id} disabled={editorBusy || Boolean(configurationEditor)} onClick={() => addComparisonConfiguration(guide.id)} className="rounded-lg border border-violet-500/30 px-3 py-2 text-xs text-violet-200 disabled:opacity-40">Add {guide.label || guide.id}</button>)}</div>

                        </section>}
                        {selectedArtifacts.length > 1 && <section className="rounded-xl border border-slate-800 p-4"><div className="flex items-center gap-2"><GitCompareArrows className="h-4 w-4 text-violet-300" /><h3 className="text-xs font-bold">Configuration Differences</h3></div><div className="mt-3 overflow-x-auto"><table className="w-full text-left text-[10px]"><thead className="text-slate-500"><tr><th className="py-2">Parameter</th>{artifactSummaries.map((_, index) => <th key={index}>Configuration {String.fromCharCode(65 + index)}</th>)}</tr></thead><tbody className="divide-y divide-slate-800"><tr><td className="py-2 text-slate-500">Guide</td>{artifactSummaries.map((item, index) => <td key={index}>{item.guide}</td>)}</tr><tr><td className="py-2 text-slate-500">Model</td>{artifactSummaries.map((item, index) => <td key={index}>{item.model}</td>)}</tr><tr><td className="py-2 text-slate-500">Topology</td>{artifactSummaries.map((item, index) => <td key={index}>{item.topology}</td>)}</tr><tr><td className="py-2 text-slate-500">Accelerators</td>{selectedArtifacts.map((item, index) => <td key={index}>{configurationFacts(item, providers.get(item.deployable_configuration?.provider_ref)).gpu || '—'}</td>)}</tr></tbody></table></div></section>}
                </div>}

                {!deployOnly && step === 2 && <BenchmarkInputs
                    selectedArtifacts={selectedArtifacts}
                    targetMode={targetMode}
                    benchmark={effectiveBenchmark} setBenchmark={setCustomBenchmark}
                    workloadMode={workloadMode} selectWorkloadMode={selectWorkloadMode}
                    slaTargets={slaTargets} setSlaTargets={setSlaTargets}
                    targetCount={runCount} summary={artifactSummaries.map(item => `${item.guide} · ${item.model} · ${item.topology}`).join("; ")}
                    issues={benchmarkErrors} recommendedBenchmark={recommendedBenchmark}
                />}

                {!deployOnly && step === 3 && <EvaluationPlan
                    configurations={selectedArtifacts.map((artifact) => ({
                        ...configurationFacts(artifact, providers.get(artifact.deployable_configuration?.provider_ref)),
                        id: artifact.artifact_id,
                        optimizationSelection: selectionFor(artifact),
                        image: artifactContent(artifact).runtime?.image,
                        parameters: (artifactContent(artifact).customParameters || artifactContent(artifact).custom_parameters || [])
                            .map(parameter => `${parameter.target || 'both'}: ${parameter.name}=${parameter.value}`).join(' · '),
                    }))}
                    baselines={[]}
                    benchmark={effectiveBenchmark}
                    slaTargets={slaTargets}
                    cluster={cluster.name || cluster.id}
                    existingEndpoint={targetMode === 'existing' ? (modelServices.find(item => item.id === targetId)?.name || targetId || 'Existing endpoint') : null}
                    preserveDeployment={preserveDeployment}
                    onEditSetup={() => setStep(0)}
                    onEditConfigurations={() => setStep(1)}
                    onEditBenchmark={() => setStep(2)}
                />}
            </main>
        </div>}
        <Modal isOpen={Boolean(yamlArtifact)} onClose={() => setYamlArtifact(null)} title="Deployment Configuration YAML" subtitle="YAML editing supports annotations and formatting. Use Edit configuration for model, topology, runtime or storage changes." size="xl" footer={<>{yamlArtifact?.deployable_configuration?.content?.officialGuide?.deploymentBundle && <a className="mr-auto text-xs text-cyan-300 hover:underline" href={`/api/v1/configurations/artifacts/${encodeURIComponent(yamlArtifact.artifact_id)}/bundle`}>Download deployment bundle</a>}<Button variant="secondary" onClick={() => setYamlArtifact(null)}>Cancel</Button><Button disabled={yamlLoading || yamlSaving || !yamlText.trim()} onClick={saveEditedYaml}>{yamlSaving ? "Saving…" : "Save YAML"}</Button></>}>
            {yamlLoading ? <div className="flex h-72 items-center justify-center"><RefreshCw className="h-5 w-5 animate-spin text-cyan-300" /></div> : <textarea aria-label="Editable deployment YAML" value={yamlText} onChange={(event) => setYamlText(event.target.value)} spellCheck={false} className="h-[60vh] w-full resize-none rounded-xl border border-slate-700 bg-black/35 p-4 font-mono text-[10px] leading-5 text-slate-300 outline-none focus:border-cyan-500" />}
        </Modal>
        {!deployOnly && !(step === 1 && configurationEditor) && <footer className="grid grid-cols-1 items-center gap-4 py-4 sm:grid-cols-[minmax(0,1fr)_auto]">
            <p className={`min-w-0 break-words text-xs leading-5 ${error ? "text-rose-300" : busy ? "text-cyan-300" : "text-amber-300"}`}>{error ? "Review the issue above, then retry." : (busy ? "Creating evaluation and scheduling deployment…" : step === 0 ? (!canContinue && (targetMode === "existing" ? "Select an endpoint" : `Required: ${setupIssues.join(" · ")}`)) : step === 1 ? ((!canContinue || combinationErrors.length > 0) && (combinationErrors[0] || "Add or select a valid configuration")) : step === 2 ? (canContinue ? `${tests.length ? runCount / tests.length : 0} targets × ${tests.length} tests = ${runCount} runs · Sequential` : benchmarkErrors[0] || "Benchmark Plan has no selected tests") : null)}</p>
            <div className="flex min-w-0 flex-wrap items-center justify-end gap-2 sm:max-w-lg">
            {targetMode === "configurations" && step >= 2 && <label className="mr-2 inline-flex min-h-9 items-center gap-2 rounded-lg bg-slate-800/50 px-3 text-xs text-slate-300"><input type="checkbox" checked={preserveDeployment} onChange={(event) => setPreserveDeployment(event.target.checked)} />Keep successful deployments</label>}
            {step > 0 && <button onClick={() => setStep((current) => targetMode === "existing" && current === 2 ? 0 : current - 1)} className="inline-flex h-9 items-center gap-1 rounded-lg border border-slate-700 px-4 text-xs"><ChevronLeft className="h-4 w-4" />Back</button>}
            {step < 3 ? <button disabled={combinationErrors.length > 0 && step > 0 || !canContinue} onClick={continueWizard} className="inline-flex h-9 items-center gap-1 rounded-lg bg-cyan-400 px-4 text-xs font-bold text-slate-950 disabled:opacity-40">{step === 0 && configurationEditor && targetMode === "configurations" ? "Return to configuration" : step === 1 ? "Continue to Benchmark" : "Continue"}<ChevronRight className="h-4 w-4" /></button> : <button disabled={busy} onClick={create} className="inline-flex h-10 min-w-40 shrink-0 items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-cyan-300 to-sky-400 px-5 text-xs font-bold text-slate-950 shadow-lg shadow-cyan-500/15 transition hover:brightness-110 disabled:cursor-wait disabled:opacity-70">{busy ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}{busy ? "Creating…" : "Start Evaluation"}</button>}
            </div>
        </footer>}
    </section>;
}
