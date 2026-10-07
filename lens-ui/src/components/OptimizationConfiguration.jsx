import { validateImportManifest } from '../features/evaluation/importManifest.js';
import ResourceBasisControl from './evaluation/ResourceBasisControl.jsx';
import OptimizationComparisonTree from './evaluation/OptimizationComparisonTree.jsx';
import { recommendationBudget, selectedOptimizationPlan, withRecommendationBudget } from '../features/evaluation/experimentDesign.js';
// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Configuration resource page (GPUStack-style). Produce a deployable
// configuration from an llm-d guide template (primary) or an AIC prediction
// (optional recommendation), then publish an immutable artifact for Evaluate.

import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, ArrowLeft, ChevronRight, FileCode, Loader2, Send, Trash2, Upload, WandSparkles, X } from 'lucide-react';
import { loadGuideCatalog, planGuideDeployment, prepareGuideSource } from './OptimizationWorkspace/guidePlanningBackend';
import { checkAicSupport, searchAicCandidates } from './OptimizationWorkspace/candidateBackend';
import { loadClusterOverview, loadClusters, selectCluster } from './OptimizationWorkspace/clusterBackend';
import { loadConfigurationCapabilities, renderConfiguration, saveConfiguration } from './OptimizationWorkspace/configurationBackend';
import { MultiSelectDropdown } from './common/MultiSelectDropdown';
import { listStorageVolumes } from './StorageManagement/storageManagementBackend';
import { listModelCacheEntries } from './ModelCache/modelCacheBackend';
import { sourceRepository } from './ModelCache/modelCachePresentation';
import { configurationCustomRows, expandConfigurationTopologies, runtimeControlOverrides, configurationRuntimeValues } from '../features/evaluation/configuration';
import { normalizeGuideSettings } from '../features/evaluation/guideSettings';
import { createPlanningSession } from '../features/evaluation/planningSession';
import ConfigurationField, { FieldHelp } from './evaluation/ConfigurationField';
import { ConfigurationValidationContext, focusConfigurationError } from './evaluation/configurationFieldState';
import { validateConfigurationInputs, configurationServerErrors } from '../features/evaluation/configurationValidation';
import { TopologyPreview, YamlPreview } from './evaluation/ConfigurationPreview';
import { StorageVolumeSelect } from './common/StorageVolumeSelect';
import { acceleratorVariantForHardware, DEFAULT_RUNTIME_IMAGES, isDefaultRuntimeImage } from './benchmark-results/acceleratorDisplay';

const PREFILL_KEY = 'prism_evaluate_prefill_workloads';
const ARTIFACT_KEY = 'prism_evaluate_configuration_artifact';
const INTENT_KEY = 'prism_evaluate_pending_intent';
const MODEL_MARKET_DRAFT_KEY = 'prism_model_market_draft';
const DEFAULT_RUNTIME_IMAGE = DEFAULT_RUNTIME_IMAGES.xpu;

const inputClass =
    'mt-1 h-9 w-full border border-slate-700 bg-slate-950 px-2 text-xs text-slate-100 outline-none focus:border-cyan-500';
const labelClass = 'text-[11px] font-medium uppercase tracking-wide text-slate-400';
const normalizeModelRepository = (value) => String(value || '').split('@')[0].trim().toLowerCase();

const PRECISE_DEFAULT_RATE_STAGES = [
    [1, 20], [2, 20], [4, 20],
];

const defaultBenchmark = (guide = '') => ({
    harness: 'inference-perf',
    workload: 'sanity_random.yaml',
    parallelism: 1,
    wait_timeout_seconds: 1800,
    ...(guide === 'pd-disaggregation' ? {
        matrix: [{ isl: 1024, osl: 128 }],
        concurrency_stages: [
            { concurrency: 1, num_requests: 8 },
            { concurrency: 4, num_requests: 16 },
        ],
        warmup_requests: 1,
    } : guide === 'precise-prefix-cache-routing' ? {
        // Keep the default suitable for validation on a development cluster. Larger
        // saturation sweeps remain available by editing these values in Evaluation.
        shared_prefix: {
            num_groups: 12,
            num_prompts_per_group: 2,
            system_prompt_len: 1024,
            question_len: 128,
            output_len: 128,
            enable_multi_turn_chat: false,
            interval: 10,
            stages: PRECISE_DEFAULT_RATE_STAGES.map(([rate, duration]) => ({ rate, duration })),
        },
    } : {}),
});

function slug(value) {
    return String(value || 'workload')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/(^-|-$)/g, '')
        .slice(0, 40) || 'workload';
}

function estimateModelResources(modelName, acceleratorCount, acceleratorMemoryGiB = 24) {
    const match = String(modelName || '').match(/(?:^|[-_/])(\d+(?:\.\d+)?)b(?:[-_/]|$)/i);
    if (!match) return null;
    const parametersBillions = Number(match[1]);
    const estimatedWeightMemoryGiB = Number((parametersBillions * 2 * 1.2 * 1e9 / 1024 ** 3).toFixed(1));
    const recommendedTp = Math.max(1, Math.ceil(estimatedWeightMemoryGiB / (acceleratorMemoryGiB * 0.9)));
    return {
        parametersBillions,
        estimatedWeightMemoryGiB,
        acceleratorMemoryGiB,
        recommendedTp,
        maxReplicas: acceleratorCount > 0 ? Math.floor(acceleratorCount / recommendedTp) : null,
    };
}

function sendWorkloadsToEvaluate(workloads, artifact, onNavigate) {
    sessionStorage.setItem(PREFILL_KEY, JSON.stringify(workloads));
    sessionStorage.setItem(ARTIFACT_KEY, JSON.stringify(artifact));
    onNavigate('optimization-evaluate');
}

function persistClusterSession(cluster) {
    if (!cluster) {
        sessionStorage.removeItem('prism_cluster_session_id');
        sessionStorage.removeItem('prism_cluster_server_id');
        sessionStorage.removeItem('prism_cluster_connection');
        return;
    }
    sessionStorage.setItem('prism_cluster_session_id', cluster.sessionId);
    sessionStorage.setItem('prism_cluster_server_id', cluster.serverId);
    sessionStorage.setItem('prism_cluster_connection', JSON.stringify(cluster));
}

async function resolveClusterSession(clusterId) {
    const payload = await selectCluster(clusterId);
    if (!payload?.sessionId || !payload?.cluster?.id) {
        throw new Error('Cluster API did not return an active session');
    }
    const cluster = {
        sessionId: payload.sessionId,
        serverId: payload.cluster.id,
        name: payload.cluster.name || payload.cluster.id,
        transport: 'managed-kubeconfig',
    };
    return cluster;
}



export default function OptimizationConfiguration({ onNavigate, onCancel, onPublished, sharedContext, initialGuide, initialArtifact, initialOptimizationSelection, editorResources, onBusyChange, singleConfiguration = false, embedded = false }) {
    const [modelMarketDraft] = useState(() => {
        if (embedded) return null;
        try {
            const draft = JSON.parse(sessionStorage.getItem(MODEL_MARKET_DRAFT_KEY) || 'null');
            sessionStorage.removeItem(MODEL_MARKET_DRAFT_KEY);
            return draft;
        } catch {
            sessionStorage.removeItem(MODEL_MARKET_DRAFT_KEY);
            return null;
        }
    });
    const [clusters, setClusters] = useState([]);
    const [clustersLoading, setClustersLoading] = useState(true);
    const [clusterSessionLoading, setClusterSessionLoading] = useState(false);
    const [clusterError, setClusterError] = useState('');
    const [clusterReloadToken, setClusterReloadToken] = useState(0);
    const [selectedClusterId, setSelectedClusterId] = useState(
        () => sessionStorage.getItem('prism_cluster_server_id') || '',
    );
    const [localConnectedCluster, setConnectedCluster] = useState(null);
    const sharedCluster = sharedContext?.cluster;
    const connectedCluster = useMemo(() => sharedCluster ? (sharedCluster.session_id ? {
        sessionId: sharedCluster.session_id, serverId: sharedCluster.id,
        name: sharedCluster.name, transport: sharedCluster.transport,
    } : null) : localConnectedCluster, [sharedCluster, localConnectedCluster]);
    const [localClusterHardware, setClusterHardware] = useState(null);
    const clusterHardware = sharedContext?.clusterHardware ?? localClusterHardware;
    const [resourceBasis, setResourceBasis] = useState(initialArtifact?.deployable_configuration?.content?.runtime?.resourceBasis || 'available');
    const budget = recommendationBudget(clusterHardware, resourceBasis);
    const [optimizationSelection, setOptimizationSelection] = useState(initialOptimizationSelection || ['full']);
    const [clusterHardwareLoading, setClusterHardwareLoading] = useState(false);

    // Shared model context.
    const [localModel, setModel] = useState('Qwen/Qwen3-0.6B');
    const model = sharedContext?.model ?? localModel;
    const [localModelSource, setModelSource] = useState('auto-cache');
    const modelSource = sharedContext?.modelSource ?? localModelSource;
    const [localModelPath, setModelPath] = useState('/mnt/data/huggingface-cache');
    const modelPath = sharedContext?.modelPath ?? localModelPath;
    const [localDeploymentName, setDeploymentName] = useState('');
    const deploymentName = sharedContext?.deploymentName ?? localDeploymentName;
    // "auto-cache" no longer accepts a free-typed path: it must come from a
    // registered Model Cache storage volume so the model's presence there can
    // actually be verified before allowing Generate/Deploy. `local-disk`
    // volumes patch a raw hostPath volume into the previewed Guide manifest
    // (see server/guidePlanning.ts); `nfs`/`dynamic-pvc` volumes patch a
    // `persistentVolumeClaim` volume referencing the volume's already
    // provisioned `pvcName` instead (same PVC the real deploy path resolves
    // via `runtime.storageVolumeId` -> `storage_mount.py::resolve_mount()`).
    const [localModelCacheVolumeId, setModelCacheVolumeId] = useState('');
    const modelCacheVolumeId = sharedContext?.storageVolumeId ?? localModelCacheVolumeId;
    const [modelCacheVolumes, setModelCacheVolumes] = useState([]);
    const [modelCacheVolumesLoading, setModelCacheVolumesLoading] = useState(false);
    const [modelCacheVolumesError, setModelCacheVolumesError] = useState('');
    const [modelCacheEntries, setModelCacheEntries] = useState([]);
    const [modelCacheEntriesLoading, setModelCacheEntriesLoading] = useState(false);
    const [modelCacheEntriesError, setModelCacheEntriesError] = useState('');

    // Guide-based state.
    const [catalog, setCatalog] = useState(null);
    const [catalogLoading, setCatalogLoading] = useState(true);
    const [catalogError, setCatalogError] = useState('');
    const [selection, setSelection] = useState({ guide: '', accelerator: '', modelServer: '' });
    const [guideSourceMode, setGuideSourceMode] = useState('official');
    const [localGuidePath, setLocalGuidePath] = useState('');
    const [remoteGuideRepository, setRemoteGuideRepository] = useState('llm-d/llm-d');
    const [remoteGuideRef, setRemoteGuideRef] = useState('main');
    const [remoteGuidePath, setRemoteGuidePath] = useState('');
    const editorRef = useRef(null);
    const generatedYamlEndRef = useRef(null);
    const [validationSubmitted, setValidationSubmitted] = useState(false);
    const [serverFieldErrors, setServerFieldErrors] = useState([]);
    const [replicaVariants, setReplicaVariants] = useState(() => String(sharedContext?.replicas || 1));
    const replicas = Number(replicaVariants.split(',')[0]);
    const setReplicas = value => setReplicaVariants(String(value));
    const [tensorParallelVariants, setTensorParallelVariants] = useState(() => String(sharedContext?.tensorParallelSize || 1));
    const tensorParallelSize = Number(tensorParallelVariants.split(',')[0]);
    const setTensorParallelSize = value => setTensorParallelVariants(String(value));
    const [prefillReplicaVariants, setPrefillReplicaVariants] = useState(() => String(sharedContext?.prefillReplicas || 1));
    const prefillReplicas = Number(prefillReplicaVariants.split(',')[0]);
    const setPrefillReplicas = value => setPrefillReplicaVariants(String(value));
    const [prefillTensorParallelVariants, setPrefillTensorParallelVariants] = useState(() => String(sharedContext?.prefillTensorParallelSize || 1));
    const prefillTensorParallelSize = Number(prefillTensorParallelVariants.split(',')[0]);
    const setPrefillTensorParallelSize = value => setPrefillTensorParallelVariants(String(value));
    const [pdTopologyVariants, setPdTopologyVariants] = useState('');
    const [guideVariant, setGuideVariant] = useState('vllm');
    const [customRows, setCustomRows] = useState([]);
    const [guideSettings, setGuideSettings] = useState({});
    const customParametersText = JSON.stringify(customRows);
    const [maxModelLen, setMaxModelLen] = useState('');
    const [maxNumSeqs, setMaxNumSeqs] = useState('');
    const [gpuMemoryUtilization, setGpuMemoryUtilization] = useState('');
    const [blockSize, setBlockSize] = useState('');
    const [maxNumBatchedTokens, setMaxNumBatchedTokens] = useState('');
    const [localImageMode, setImageMode] = useState(() => sharedContext?.imageMode || 'use-upstream-image');
    const imageMode = localImageMode;
    const [localImage, setImage] = useState(() => sharedContext?.image || DEFAULT_RUNTIME_IMAGE);
    const image = localImage;
    const [localBuildSourceUrl, setBuildSourceUrl] = useState('');
    const buildSourceUrl = sharedContext?.buildSourceUrl ?? localBuildSourceUrl;
    const [planning, setPlanning] = useState(false);
    const [planResult, setPlanResult] = useState(null);
    useEffect(() => {
        if (!embedded || !planResult) return undefined;
        const frame = window.requestAnimationFrame(() => {
            generatedYamlEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
        });
        return () => window.cancelAnimationFrame(frame);
    }, [embedded, planResult]);
    const [planError, setPlanError] = useState('');
    const [uploadingConfiguration, setUploadingConfiguration] = useState(false);
    const [uploadError, setUploadError] = useState('');

    // AIC state.
    const [aicSystem] = useState('b60');
    const [aicBackend] = useState('vllm');
    const [aicLoading, setAicLoading] = useState(false);
    const [aicError, setAicError] = useState('');
    const [aicCandidates, setAicCandidates] = useState(null);
    const [pdRecommendation, setPdRecommendation] = useState(false);
    const [publishing, setPublishing] = useState(false);
    const [publishError, setPublishError] = useState('');
    const [planInSession] = useState(() => createPlanningSession(planGuideDeployment));
    const [progress, setProgress] = useState('');
    const [elapsedSeconds, setElapsedSeconds] = useState(0);
    const operationBusy = planning || publishing || uploadingConfiguration;
    useEffect(() => {
        onBusyChange?.(operationBusy);
        return () => onBusyChange?.(false);
    }, [operationBusy, onBusyChange]);
    useEffect(() => {
        if (!operationBusy) return undefined;
        const started = Date.now();
        setElapsedSeconds(0);
        const timer = window.setInterval(() => setElapsedSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
        return () => window.clearInterval(timer);
    }, [operationBusy]);
    const [pendingIntent] = useState(() => {
        try {
            return JSON.parse(sessionStorage.getItem(INTENT_KEY) || 'null');
        } catch {
            return null;
        }
    });
    const [initialTarget] = useState(() => ({
        guide: initialGuide || pendingIntent?.workloads?.[0]?.guide,
        variant: initialArtifact?.deployable_configuration?.content?.guideVariant || initialArtifact?.deployable_configuration?.content?.guide_variant,
        modelServer: sharedContext?.modelServer || pendingIntent?.runtime?.model_server,
    }));
    const [baselineTypes, setBaselineTypes] = useState(
        () => pendingIntent?.baseline_types || (pendingIntent?.baseline_type ? [pendingIntent.baseline_type] : []),
    );
    const returnFromConfiguration = () => {
        if (onCancel) onCancel();
        else onNavigate(pendingIntent?.return_target || 'optimization-evaluate');
    };

    useEffect(() => {
        if (sharedContext?.cluster?.id) setSelectedClusterId(sharedContext.cluster.id);
        setPlanResult(null);
    }, [sharedContext?.cluster?.id, model, modelSource, modelPath, modelCacheVolumeId, imageMode, image, buildSourceUrl, deploymentName]);

    useEffect(() => {
        const content = initialArtifact?.deployable_configuration?.content;
        if (!content) return;
        const editorSource = initialArtifact.deployable_configuration.provenance?.editor_source;
        if (editorSource) {
            setGuideSourceMode(editorSource.guideSourceMode || 'official');
            setLocalGuidePath(editorSource.localGuidePath || '');
            setRemoteGuideRepository(editorSource.remoteGuideRepository || '');
            setRemoteGuideRef(editorSource.remoteGuideRef || 'main');
            setRemoteGuidePath(editorSource.remoteGuidePath || '');
        }
        const decode = content.decode || content.serving || {};
        const prefill = content.prefill || {};
        const replicas = decode.replicaCount ?? decode.replicas ?? 1;
        const tp = decode.tensorParallelSize ?? decode.tensor_parallel_size ?? 1;
        const prefillReplicas = prefill.replicaCount ?? prefill.replicas ?? 1;
        const prefillTp = prefill.tensorParallelSize ?? prefill.tensor_parallel_size ?? 1;
        setReplicas(replicas);
        setReplicaVariants(String(replicas));
        setTensorParallelSize(tp);
        setTensorParallelVariants(String(tp));
        setPrefillReplicas(prefillReplicas);
        setPrefillReplicaVariants(String(prefillReplicas));
        setPrefillTensorParallelSize(prefillTp);
        setPrefillTensorParallelVariants(String(prefillTp));
        setGuideVariant(content.guideVariant || content.guide_variant || 'vllm');
        const values = configurationRuntimeValues(content);
        setMaxModelLen(values.maxModelLen);
        setMaxNumSeqs(values.maxNumSeqs);
        setGpuMemoryUtilization(values.gpuMemoryUtilization);
        setBlockSize(values.blockSize);
        setMaxNumBatchedTokens(values.maxNumBatchedTokens);
        setCustomRows(configurationCustomRows(content));
        setGuideSettings(content.guideSettings || {});
    }, [initialArtifact]);

    useEffect(() => {
        if (!modelMarketDraft) return;
        const parsedReplicas = Number(modelMarketDraft.replicas);
        const parsedTp = Number(modelMarketDraft.tp);
        setModel(modelMarketDraft.model || 'Qwen/Qwen3-0.6B');
        setDeploymentName(modelMarketDraft.deployName || '');
        setModelSource('auto-cache');
        setModelPath('/mnt/data/huggingface-cache');
        setSelectedClusterId(modelMarketDraft.cluster || '');
        // Carry over the storage volume chosen on Model Market's one-click
        // deployment form -- the volumes list for this cluster hasn't loaded
        // yet, but the effect below re-selects it once fetched as long as it
        // still exists and is ready (same fallback logic as manual picking).
        if (modelMarketDraft.storageVolumeId) setModelCacheVolumeId(modelMarketDraft.storageVolumeId);
        if (Number.isInteger(parsedReplicas) && parsedReplicas > 0) {
            setReplicas(parsedReplicas);
            setReplicaVariants(String(parsedReplicas));
        }
        if (Number.isInteger(parsedTp) && parsedTp > 0) {
            setTensorParallelSize(parsedTp);
            setTensorParallelVariants(String(parsedTp));
        }
        setMaxModelLen(modelMarketDraft.model?.includes('deepseek') ? 8192 : 4096);
    }, [modelMarketDraft]);
    useEffect(() => {
        if (embedded) { setClustersLoading(false); return undefined; }
        let active = true;
        setClustersLoading(true);
        loadClusters()
            .then((payload) => {
                if (!active) return;
                const items = Array.isArray(payload?.items) ? payload.items : [];
                setClusters(items);
                setSelectedClusterId((current) => {
                    if (items.some((cluster) => cluster.id === current && cluster.ready)) return current;
                    return items.find((cluster) => cluster.ready)?.id || '';
                });
                setClusterError('');
            })
            .catch((error) => {
                if (!active) return;
                setClusters([]);
                setSelectedClusterId('');
                setConnectedCluster(null);
                persistClusterSession(null);
                setClusterError(error.message || 'Could not load created clusters');
            })
            .finally(() => active && setClustersLoading(false));
        return () => { active = false; };
    }, [clusterReloadToken, embedded]);

    useEffect(() => {
        if (embedded) return undefined;
        if (!selectedClusterId) {
            setConnectedCluster(null);
            setClusterHardware(null);
            persistClusterSession(null);
            return undefined;
        }
        let active = true;
        setConnectedCluster(null);
        setClusterHardware(null);
        setClusterSessionLoading(true);
        setClusterHardwareLoading(true);
        setClusterError('');
        resolveClusterSession(selectedClusterId)
            .then((cluster) => {
                if (!active) return;
                persistClusterSession(cluster);
                setConnectedCluster(cluster);
                loadClusterOverview(selectedClusterId)
                    .then((overview) => active && setClusterHardware(overview?.kubernetes?.hardware || null))
                    .catch(() => active && setClusterHardware(null))
                    .finally(() => active && setClusterHardwareLoading(false));
            })
            .catch((error) => {
                if (!active) return;
                persistClusterSession(null);
                setClusterHardware(null);
                setClusterHardwareLoading(false);
                setClusterError(error.message || 'Could not activate the selected cluster');
            })
            .finally(() => {
                if (!active) return;
                setClusterSessionLoading(false);
            });
        return () => { active = false; };
    }, [selectedClusterId, embedded]);

    // Model Cache storage volumes registered for the selected cluster --
    // "auto-cache" mode must pick one of these instead of typing a raw path,
    // so the model's actual presence can be verified (see effect below).
    // All volume kinds are offered: `local-disk` resolves to a raw hostPath
    // patch, `nfs`/`dynamic-pvc` resolve to the volume's already-provisioned
    // PVC (`pvcName`) -- both are handled by the picker/derivation below.
    useEffect(() => {
        if (embedded) return undefined;
        if (modelSource !== 'auto-cache' || !selectedClusterId) {
            setModelCacheVolumes([]);
            setModelCacheVolumesError('');
            return undefined;
        }
        let active = true;
        setModelCacheVolumesLoading(true);
        setModelCacheVolumesError('');
        // Fetch every status (not just "ready") so the picker can show
        // non-ready volumes as visible-but-unselectable instead of hiding
        // them entirely.
        listStorageVolumes({ clusterId: selectedClusterId, purpose: 'model-cache' })
            .then((items) => {
                if (!active) return;
                setModelCacheVolumes(items);
                setModelCacheVolumeId((current) => {
                    if (items.some((item) => item.id === current && item.status === 'ready')) return current;
                    return items.find((item) => item.status === 'ready')?.id || '';
                });
            })
            .catch((error) => {
                if (!active) return;
                setModelCacheVolumes([]);
                setModelCacheVolumesError(error.message || 'Could not load Model Cache storage volumes');
            })
            .finally(() => active && setModelCacheVolumesLoading(false));
        return () => { active = false; };
    }, [modelSource, selectedClusterId, embedded]);

    // Derive the hostPath (local-disk) from the selected Model Cache volume --
    // sent as `modelPath`/`mountPath` exactly as before, just no longer
    // hand-typed by the user. `nfs`/`dynamic-pvc` volumes have no hostPath;
    // their PVC is referenced instead via `storageVolumeId` in the requests
    // built below (planningRequest/candidateConfig.runtime).
    useEffect(() => {
        if (modelSource !== 'auto-cache') return;
        const volume = modelCacheVolumes.find((item) => item.id === modelCacheVolumeId);
        setModelPath(volume?.localDisk?.hostPath || '');
    }, [modelSource, modelCacheVolumeId, modelCacheVolumes]);


    // Verify the requested model is actually downloaded (status "ready") onto
    // the selected storage volume -- if not, the volume cannot be used to
    // complete this configuration until an admin downloads it via the Model
    // Cache page (see generation and publishing validation below).
    useEffect(() => {
        if (embedded) return undefined;
        if (modelSource !== 'auto-cache' || !modelCacheVolumeId) {
            setModelCacheEntries([]);
            setModelCacheEntriesError('');
            return undefined;
        }
        let active = true;
        setModelCacheEntriesLoading(true);
        setModelCacheEntriesError('');
        listModelCacheEntries({ clusterId: selectedClusterId, storageVolumeId: modelCacheVolumeId })
            .then((items) => {
                if (!active) return;
                setModelCacheEntries(items);
            })
            .catch((error) => {
                if (!active) return;
                setModelCacheEntries([]);
                setModelCacheEntriesError(error.message || 'Could not check Model Cache entries for this storage volume');
            })
            .finally(() => active && setModelCacheEntriesLoading(false));
        return () => { active = false; };
    }, [modelSource, modelCacheVolumeId, selectedClusterId, embedded]);

    useEffect(() => {
        const requested = pendingIntent?.workloads?.[0];
        if (!requested || embedded) return;
        setModel(requested.model || 'Qwen/Qwen3-0.6B');
        setReplicas(requested.decode_replicas || requested.replicas || 1);
        setTensorParallelSize(requested.decode_tensor_parallel_size || requested.tensor_parallel_size || 1);
        setReplicaVariants(String(requested.decode_replicas || requested.replicas || 1));
        setTensorParallelVariants(String(requested.decode_tensor_parallel_size || requested.tensor_parallel_size || 1));
        setImageMode(pendingIntent?.runtime?.image_mode || 'use-upstream-image');
        setImage((current) => pendingIntent?.runtime?.image || pendingIntent?.runtime?.build_image_name || current);
        setBuildSourceUrl(pendingIntent?.runtime?.build_source_url || '');
        const requestedPath = pendingIntent?.runtime?.mount_path || requested.mount_path || '';
        setModelPath(requestedPath);
        setModelSource(pendingIntent?.runtime?.model_source || (requestedPath ? 'shared-path' : 'auto-cache'));
        setModelCacheVolumeId(pendingIntent?.runtime?.storage_volume_id || '');
        setDeploymentName(pendingIntent?.deployment_name || '');
    }, [pendingIntent, embedded]);

    useEffect(() => {
        let active = true;
        setCatalogLoading(true);
        setCatalog(null);
        setCatalogError('');
        (editorResources ? Promise.resolve([editorResources.catalog, editorResources.capabilities]) : Promise.all([loadGuideCatalog({ clusterId: selectedClusterId }), loadConfigurationCapabilities()]))
            .then(([payload, capabilityPayload]) => {
                if (!active) return;
                const supported = new Map((capabilityPayload.providers || []).map((item) => [item.id, item]));
                const annotated = {
                    ...payload,
                    guides: (payload.guides || []).map((guide) => {
                        const deploymentCapability = supported.get(guide.id) || null;
                        const allowedAccelerators = new Set(deploymentCapability?.accelerators || []);
                        const allowedModelServers = new Set(deploymentCapability?.model_servers || []);
                        return {
                            ...guide,
                            deploymentCapability,
                            accelerators: deploymentCapability ? (guide.accelerators || [])
                                .filter((accelerator) => allowedAccelerators.has(accelerator.id))
                                .map((accelerator) => ({
                                    ...accelerator,
                                    modelServers: (accelerator.modelServers || []).filter((server) => allowedModelServers.has(server.id)),
                                }))
                                .filter((accelerator) => accelerator.modelServers.length) : guide.accelerators,
                        };
                    }),
                };
                setCatalog(annotated);
                const requestedGuide = initialTarget.guide;
                const guide = annotated.guides.find((item) => item.id === requestedGuide && item.deploymentCapability)
                    || annotated.guides.find((item) => item.deploymentCapability);
                const accelerator = guide?.accelerators?.[0];
                const modelServer = accelerator?.modelServers?.find((item) => item.id.toLowerCase() === String(initialTarget.modelServer).toLowerCase()) || accelerator?.modelServers?.[0];
                setSelection({
                    guide: guide?.id || '',
                    accelerator: accelerator?.id || '',
                    modelServer: initialTarget.modelServer?.toLowerCase() || modelServer?.id || '',
                });
                // Pick a variant valid for the selected cluster's accelerator, so
                // an NVIDIA cluster never keeps the Intel XPU `vllm`/`vllm-rdma`.
                const capability = guide?.deploymentCapability;
                const requestedVariant = initialTarget.variant;
                let defaultVariant = requestedVariant || '';
                if (guide?.id === 'tiered-prefix-cache') {
                    const tieredVariants = capability?.variants || [];
                    defaultVariant = requestedVariant && tieredVariants.includes(requestedVariant)
                        ? requestedVariant
                        : (capability?.evaluation?.default_variant || tieredVariants[0] || '');
                } else if (guide?.id === 'pd-disaggregation') {
                    const pdVariants = capability?.variants_by_accelerator?.[accelerator?.id] || [];
                    defaultVariant = requestedVariant && pdVariants.includes(requestedVariant)
                        ? requestedVariant
                        : (pdVariants[0] || '');
                }
                setGuideVariant(defaultVariant);
                if (!pendingIntent?.baseline_types && !pendingIntent?.baseline_type) {
                    setBaselineTypes(guide?.id === 'precise-prefix-cache-routing' ? ['kubernetes-service'] : []);
                }
            })
            .catch((error) => active && setCatalogError(error.message || 'Could not load guide catalog'))
            .finally(() => active && setCatalogLoading(false));
        return () => { active = false; };
    }, [pendingIntent, initialTarget, editorResources, selectedClusterId]);

    const guides = catalog?.guides || [];
    const selectedGuide = guides.find((item) => item.id === selection.guide);
    const accelerators = useMemo(() => selectedGuide?.accelerators || [], [selectedGuide]);
    const selectedAccelerator = accelerators.find((item) => item.id === selection.accelerator);
    const modelServers = selectedAccelerator?.modelServers || [];
    const selectedModelServer = modelServers.find((item) => item.id === selection.modelServer);
    // Keep the Guide variant aligned with the selected cluster's hardware so a
    // GPU cluster never stays on the Intel XPU variant (and vice versa).
    useEffect(() => {
        const preferred = acceleratorVariantForHardware(clusterHardware);
        if (!preferred) return;
        const match = accelerators.find((item) => item.id === preferred);
        if (!match) return;
        if (selection.accelerator !== preferred) {
            setSelection((current) => ({
                ...current,
                accelerator: preferred,
                modelServer: current.modelServer || match.modelServers?.[0]?.id || '',
            }));
        }
        if (selection.guide === 'pd-disaggregation') {
            const variants = selectedGuide?.deploymentCapability?.variants_by_accelerator?.[preferred] || [];
            if (variants.length && !variants.includes(guideVariant)) setGuideVariant(variants[0]);
        }
    }, [clusterHardware, selection.accelerator, selection.guide, selectedGuide, accelerators, guideVariant]);
    // Same hardware alignment for the default runtime image: swap only between
    // the known per-vendor defaults so a user-entered image is never clobbered.
    useEffect(() => {
        const variant = acceleratorVariantForHardware(clusterHardware);
        if (!variant) return;
        setImage((current) => (
            isDefaultRuntimeImage(current) && current !== DEFAULT_RUNTIME_IMAGES[variant]
                ? DEFAULT_RUNTIME_IMAGES[variant]
                : current
        ));
    }, [clusterHardware]);
    // Guide variants available for the selected cluster hardware. PD's variant
    // set depends on the accelerator (NVIDIA GPU: the vLLM infra-provider
    // overlay such as `base`; Intel XPU: `vllm` / `vllm-rdma`).
    const guideVariants = useMemo(() => {
        if (selection.guide === 'tiered-prefix-cache') return selectedGuide?.deploymentCapability?.variants || [];
        if (selection.guide === 'pd-disaggregation') {
            const capability = selectedGuide?.deploymentCapability;
            return capability?.variants_by_accelerator?.[selection.accelerator] || capability?.variants || [];
        }
        return selectedModelServer?.variants || [];
    }, [selection.guide, selectedGuide, selectedModelServer, selection.accelerator]);
    const unavailableGuideVariants = useMemo(
        () => (selection.guide === 'pd-disaggregation'
            ? selectedGuide?.deploymentCapability?.unavailable_variants_by_accelerator?.[selection.accelerator] || []
            : []),
        [selection.guide, selectedGuide, selection.accelerator],
    );
    useEffect(() => { setOptimizationSelection(selection.guide === initialTarget.guide ? initialOptimizationSelection || ['full'] : ['full']); }, [selection.guide, initialTarget.guide, initialOptimizationSelection]);
    const effectiveBaselineTypes = baselineTypes.filter((baseline) => (
        baseline === 'direct-vllm'
        || (baseline === 'optimized-baseline' && ['pd-disaggregation', 'tiered-prefix-cache'].includes(selection.guide))
        || (baseline === 'kubernetes-service' && selection.guide === 'precise-prefix-cache-routing')
        || (baseline === 'router-neutral' && selection.guide === 'optimized-baseline' && Number(replicas) >= 2)
    ));
    const launchBaselineTypes = selection.guide === 'precise-prefix-cache-routing'
        ? ['kubernetes-service', ...effectiveBaselineTypes.filter((item) => item !== 'kubernetes-service')]
        : effectiveBaselineTypes;
    const liveModelAdvice = useMemo(
        () => estimateModelResources(
            model,
            budget,
        ),
        [model, budget],
    );
    const topologyInputs = {
        guide: selection.guide,
        replicaVariants: singleConfiguration ? String(replicas) : replicaVariants,
        tensorParallelVariants: singleConfiguration ? String(tensorParallelSize) : tensorParallelVariants,
        prefillReplicaVariants: singleConfiguration ? String(prefillReplicas) : prefillReplicaVariants,
        prefillTensorParallelVariants: singleConfiguration ? String(prefillTensorParallelSize) : prefillTensorParallelVariants,
        pdTopologyVariants: singleConfiguration ? '' : pdTopologyVariants,
    };
    const guideSourceSummary = guideSourceMode === 'local'
        ? localGuidePath.trim() || 'Local guide path not set'
        : guideSourceMode === 'remote'
            ? `${remoteGuideRepository.trim() || 'Repository not set'}@${remoteGuideRef.trim() || 'ref not set'} / ${remoteGuidePath.trim() || 'guide path not set'}`
            : `${catalog?.repository || 'llm-d/llm-d'}@${catalog?.ref || 'main'} / guides / ${selection.guide} / ${selection.accelerator} / ${selection.modelServer}`;
    // Model Cache validation for "auto-cache" mode: the selected storage
    // volume can only be used once the requested model is actually present
    // and "ready" in that volume's Model Cache entries.
    const modelCacheValidation = useMemo(() => {
        if (modelSource !== 'auto-cache') return { state: 'not-applicable' };
        if (sharedContext?.cacheValidation) return sharedContext.cacheValidation;
        if (modelCacheVolumesLoading || modelCacheEntriesLoading) return { state: 'loading' };
        if (modelCacheVolumesError) return { state: 'error', message: modelCacheVolumesError };
        if (!modelCacheVolumeId) return { state: 'no-volume', message: 'Select a Model Cache storage volume registered for this cluster.' };
        if (modelCacheEntriesError) return { state: 'error', message: modelCacheEntriesError };
        const trimmed = model.trim();
        if (!trimmed) return { state: 'no-model' };
        const expected = normalizeModelRepository(trimmed);
        const ready = modelCacheEntries.some((entry) => (
            entry.status === 'ready' && normalizeModelRepository(sourceRepository(entry)) === expected
        ));
        if (!ready) {
            return {
                state: 'missing',
                message: `"${trimmed}" is not cached (ready) on this storage volume. Ask an admin to download it on the Model Cache page before using this volume.`,
            };
        }
        return { state: 'ready' };
    }, [modelSource, modelCacheVolumesLoading, modelCacheEntriesLoading, modelCacheVolumesError, modelCacheVolumeId, modelCacheEntriesError, model, modelCacheEntries, sharedContext?.cacheValidation]);

    const configurationInputErrors = validateConfigurationInputs({
        ...topologyInputs, modelServer: selection.modelServer, guideVariant, guideVariants, guideSettings, customRows,
        runtime: { maxModelLen, maxNumSeqs, gpuMemoryUtilization, blockSize, maxNumBatchedTokens },
        availableAccelerators: clusterHardware?.usableGpuCount ?? clusterHardware?.availableGpuCount,
    });
    if (embedded && onPublished && !singleConfiguration) {
        try { selectedOptimizationPlan(selection.guide, optimizationSelection); }
        catch (error) { configurationInputErrors.push({ field: 'optimizationSelection', message: error.message }); }
    }
    if (!model.trim()) configurationInputErrors.push({ field: embedded ? 'setup' : 'model', message: 'Select a model in Setup.' });
    if (!connectedCluster?.sessionId) configurationInputErrors.push({ field: embedded ? 'setup' : 'cluster', message: 'Select a ready cluster.' });
    if (!selectedModelServer) configurationInputErrors.push({ field: 'modelServer', message: 'This Guide does not support the selected runtime. Choose another Guide or edit Setup.' });
    if (modelSource === 'auto-cache' && modelCacheValidation.state !== 'ready') configurationInputErrors.push({ field: embedded ? 'setup' : 'modelCacheVolumeId', message: modelCacheValidation.message || 'Select a ready model cache in Setup.' });
    if (modelSource === 'shared-path' && (!modelPath.startsWith('/') || modelPath.split('/').includes('..'))) configurationInputErrors.push({ field: embedded ? 'setup' : 'modelPath', message: 'Enter an absolute model path without .. segments.' });
    if (guideSourceMode === 'local' && !localGuidePath.trim()) configurationInputErrors.push({ field: 'localGuidePath', message: 'Enter a local YAML or Kustomize path.' });
    if (guideSourceMode === 'remote') {
        for (const [field, value] of Object.entries({ remoteGuideRepository, remoteGuideRef, remoteGuidePath })) {
            if (!value.trim()) configurationInputErrors.push({ field, message: 'Complete this reference source field.' });
        }
    }
    const fieldErrors = [...configurationInputErrors, ...serverFieldErrors];

    useEffect(() => {
        if (!sharedContext?.modelServer) return;
        setSelection((current) => ({ ...current, modelServer: sharedContext.modelServer.toLowerCase() }));
        setPlanResult(null);
    }, [sharedContext?.modelServer]);

    const selectGuide = (guide) => {
        setGuideSettings({});
        const next = guides.find((item) => item.id === guide);
        const accelerator = next?.accelerators?.[0];
        setSelection({
            guide,
            accelerator: accelerator?.id || '',
            modelServer: sharedContext?.modelServer?.toLowerCase() || accelerator?.modelServers?.[0]?.id || '',
        });
        if (guide === 'pd-disaggregation') setGuideVariant('vllm');
        else if (guide === 'tiered-prefix-cache') setGuideVariant(next?.deploymentCapability?.variants?.includes('native/cpu/base') ? 'native/cpu/base' : next?.deploymentCapability?.variants?.[0] || '');
        else setGuideVariant('');
        setBaselineTypes(guide === 'precise-prefix-cache-routing' ? ['kubernetes-service'] : []);
    };
    const selectAccelerator = (accelerator) => {
        const next = accelerators.find((item) => item.id === accelerator);
        setSelection((current) => ({ ...current, accelerator, modelServer: sharedContext?.modelServer?.toLowerCase() || next?.modelServers?.[0]?.id || '' }));
    };

    // The volume object backing modelCacheVolumeId, when in "auto-cache" mode.
    const selectedModelCacheVolume = useMemo(
        () => (modelSource === 'auto-cache' ? sharedContext?.storageVolume || modelCacheVolumes.find((item) => item.id === modelCacheVolumeId) || null : null),
        [modelSource, modelCacheVolumes, modelCacheVolumeId, sharedContext?.storageVolume],
    );

    const requireSelectedCluster = async () => {
        if (embedded && connectedCluster?.sessionId) return connectedCluster;
        if (!selectedClusterId) throw new Error('Select an available cluster before generating a configuration');
        try {
            const current = await resolveClusterSession(selectedClusterId);
            persistClusterSession(current);
            setConnectedCluster(current);
            return current;
        } catch (error) {
            setConnectedCluster(null);
            throw error;
        }
    };

    const planningRequest = (current, replicaCount, tpCount, prefillReplicaCount = Number(prefillReplicas), prefillTpCount = Number(prefillTensorParallelSize), guideSelection = selection) => ({
        ...guideSelection,
        resourceBasis,
        recommendationGpuCount: budget,
        guideSettings: normalizeGuideSettings(guideSelection.guide, guideVariant, guideSettings),
        source: guideSourceMode === 'local'
            ? { mode: 'local', path: localGuidePath.trim() }
            : guideSourceMode === 'remote'
                ? { mode: 'remote', repository: remoteGuideRepository.trim(), ref: remoteGuideRef.trim(), path: remoteGuidePath.trim() }
                : { mode: 'official' },
        model: model.trim(),
        modelSource,
        modelPath: modelSource === 'huggingface' ? '' : modelPath.trim(),
        // `local-disk` Model Cache volumes patch a raw hostPath (modelPath above);
        // `nfs`/`dynamic-pvc` volumes have no hostPath and instead patch a
        // `persistentVolumeClaim` volume referencing the already-provisioned
        // PVC -- see server/guidePlanning.ts.
        modelStorageKind: modelSource === 'auto-cache' ? (selectedModelCacheVolume?.kind || '') : '',
        modelPvcClaimName: modelSource === 'auto-cache' ? (selectedModelCacheVolume?.pvcName || '') : '',
        replicas: replicaCount,
        tensorParallelSize: tpCount,
        prefillReplicas: guideSelection.guide === 'pd-disaggregation' ? prefillReplicaCount : 0,
        prefillTensorParallelSize: guideSelection.guide === 'pd-disaggregation' ? prefillTpCount : 0,
        decodeReplicas: replicaCount,
        decodeTensorParallelSize: tpCount,
        maxModelLen: (selection.modelServer === 'vllm' && maxModelLen !== '' ? Number(maxModelLen) : undefined),
        maxNumSeqs: (selection.modelServer === 'vllm' && maxNumSeqs !== '' ? Number(maxNumSeqs) : undefined),
        blockSize: (selection.modelServer === 'vllm' && blockSize !== '' ? Number(blockSize) : undefined), maxNumBatchedTokens: (selection.modelServer === 'vllm' && maxNumBatchedTokens !== '' ? Number(maxNumBatchedTokens) : undefined),
        customParameters: customRows,
        gpuMemoryUtilization: (selection.modelServer === 'vllm' && gpuMemoryUtilization !== '' ? Number(gpuMemoryUtilization) : undefined),
        runtimeImage: image.trim(),
        guideVariant: ['pd-disaggregation', 'tiered-prefix-cache'].includes(guideSelection.guide) ? guideVariant : '',
        clusterSessionId: current.sessionId,
        environment: { mode: 'current', kubernetesMode: 'required' },
    });

    let topologyPreviewVariants = [];
    try {
        topologyPreviewVariants = expandConfigurationTopologies(topologyInputs)
            .map((row, index) => ({ ...row, id: index + 1, prefill: row.prefillReplicaCount, prefillTp: row.prefillTpCount, decode: row.replicaCount, decodeTp: row.tpCount }));
    } catch { /* Field validation displays topology errors. */ }

    const updateCustomRows = (rows) => setCustomRows(rows);

    const previewInputs = JSON.stringify({ selection, model, modelSource, modelPath, image, guideSourceMode, localGuidePath, remoteGuideRepository, remoteGuideRef, remoteGuidePath, replicas, tensorParallelSize, prefillReplicas, prefillTensorParallelSize, replicaVariants, tensorParallelVariants, prefillReplicaVariants, prefillTensorParallelVariants, pdTopologyVariants, guideVariant, guideSettings, maxModelLen, maxNumSeqs, gpuMemoryUtilization, blockSize, maxNumBatchedTokens, customParametersText });
    useEffect(() => { setPlanResult(null); setServerFieldErrors([]); }, [previewInputs]);

    useEffect(() => {
        if (guideSourceMode !== 'official' || !selection.guide || !selection.accelerator || !selection.modelServer) return undefined;
        const timer = window.setTimeout(() => {
            prepareGuideSource({ ...selection, guideVariant, clusterId: selectedClusterId }).catch(() => {});
        }, 250);
        return () => window.clearTimeout(timer);
    }, [guideSourceMode, selection, guideVariant, selectedClusterId]);

    const showServerErrors = (messages) => {
        const mapped = configurationServerErrors(messages, { customRows, sweepEnabled: !singleConfiguration, pdTopologyVariants: topologyInputs.pdTopologyVariants, embedded });
        setServerFieldErrors(mapped);
        setValidationSubmitted(true);
        if (mapped.length) window.requestAnimationFrame(() => focusConfigurationError(editorRef.current, mapped));
        return messages.filter(message => !mapped.some(error => error.message === message));
    };

    const generate = async (requestedBasis = resourceBasis) => {
        setResourceBasis(requestedBasis);
        if (requestedBasis !== resourceBasis) {
            setAicCandidates(null);
            setPlanResult(null);
        }
        setValidationSubmitted(true);
        setServerFieldErrors([]);
        if (configurationInputErrors.length) {
            window.requestAnimationFrame(() => focusConfigurationError(editorRef.current, configurationInputErrors));
            return;
        }
        const requiredCards = Math.max(1, ...topologyPreviewVariants.map(row => row.gpuCount));
        const selectedBudget = recommendationBudget(clusterHardware, requestedBasis);
        if (selectedBudget < requiredCards) {
            setPlanResult(null);
            setPlanError(`Insufficient resources: this configuration requires ${requiredCards} cards; the selected budget is ${selectedBudget}. Reduce replicas / TP or select a sufficient budget.`);
            return;
        }
        setPlanning(true);
        setPlanError('');
        setPlanResult(null);
        setProgress('Preparing YAML and checking cluster capacity…');
        try {
            if (modelSource === 'auto-cache' && modelCacheValidation.state !== 'ready') {
                throw new Error(modelCacheValidation.message || 'Select a Model Cache storage volume that has this model ready before generating a configuration');
            }
            if (configurationInputErrors.length) throw new Error(configurationInputErrors.map(error => error.message).join(' '));
            const current = await requireSelectedCluster();
            const result = await planInSession(withRecommendationBudget(planningRequest(current, topologyPreviewVariants[0].replicaCount, topologyPreviewVariants[0].tpCount, topologyPreviewVariants[0].prefillReplicaCount, topologyPreviewVariants[0].prefillTpCount), clusterHardware, requestedBasis));
            setPlanResult(result.validation?.errors?.length ? null : result);
            const globalErrors = showServerErrors(result.validation?.errors || []);
            if (globalErrors.length) setPlanError(globalErrors.join(' '));
        } catch (error) {
            const globalErrors = showServerErrors([error.message || 'Guide planning failed']);
            setPlanError(globalErrors.join(' '));
        } finally {
            setPlanning(false);
        }
    };

    const publishWorkload = async (workload, sourceName, advance = false) => {
        setValidationSubmitted(true);
        if (configurationInputErrors.length) {
            window.requestAnimationFrame(() => focusConfigurationError(editorRef.current, configurationInputErrors));
            return;
        }
        setPublishing(true);
        setPublishError('');
        setProgress('Preparing configuration…');
        try {
            if (modelSource === 'shared-path' && (!modelPath.startsWith('/') || modelPath.split('/').includes('..'))) {
                throw new Error('Shared model path must be an absolute path available on every selected worker node');
            }
            if (modelSource === 'auto-cache' && modelCacheValidation.state !== 'ready') {
                throw new Error(modelCacheValidation.message || 'Select a Model Cache storage volume that has this model ready before deploying');
            }
            const isPd = workload.guide === 'pd-disaggregation';
            const targetCluster = await requireSelectedCluster();
            if (configurationInputErrors.length) throw new Error(configurationInputErrors.map(error => error.message).join(' '));
            const variants = expandConfigurationTopologies({ ...topologyInputs, guide: workload.guide });
            const customParameters = customRows;
            const savedArtifacts = [];
            const plans = [];
            for (const [index, variant] of variants.entries()) {
                setProgress(`Preparing YAML · configuration ${index + 1} of ${variants.length}`);
                const variantPlan = await planInSession(planningRequest(targetCluster, variant.replicaCount, variant.tpCount, variant.prefillReplicaCount, variant.prefillTpCount));
                setPlanResult(variantPlan);
                if (!variantPlan.plannedDeployment?.content) throw new Error('The Guide returned an empty configuration. Retry preparing it.');
                if (variantPlan.validation?.status === 'invalid' || variantPlan.validation?.errors?.length) {
                    throw new Error(`R${variant.replicaCount} × TP${variant.tpCount}: ${(variantPlan.validation.errors || []).join('; ')}`);
                }
                const candidateConfig = {
                    schema_version: '1.0',
                    type: isPd ? 'pd' : 'baseline',
                    guide_settings: normalizeGuideSettings(workload.guide, guideVariant, guideSettings),
                    candidate_source: { name: sourceName },
                    target: { model: workload.model, cluster_id: targetCluster.serverId, cluster_name: targetCluster.name },
                    serving: { replicas: variant.replicaCount, tensor_parallel_size: variant.tpCount, max_model_len: (selection.modelServer === 'vllm' && maxModelLen !== '' ? Number(maxModelLen) : undefined) },
                    prefill: isPd ? { replicas: variant.prefillReplicaCount, tensor_parallel_size: variant.prefillTpCount } : null,
                    decode: { replicas: variant.replicaCount, tensor_parallel_size: variant.tpCount, max_model_len: (selection.modelServer === 'vllm' && maxModelLen !== '' ? Number(maxModelLen) : undefined), max_num_seqs: (selection.modelServer === 'vllm' && maxNumSeqs !== '' ? Number(maxNumSeqs) : undefined) },
                    custom_parameters: [
                        ...runtimeControlOverrides({ maxModelLen, maxNumSeqs, gpuMemoryUtilization, blockSize, maxNumBatchedTokens }, selection.modelServer),
                        ...customParameters,
                    ],
                    guide_variant: ['pd-disaggregation', 'tiered-prefix-cache'].includes(workload.guide) ? guideVariant : undefined,
                    runtime: {
                        image, imageMode, buildSourceUrl, modelServer: selection.modelServer, resourceBasis,
                        modelPvcClaimName: selectedModelCacheVolume?.pvcName || '',
                        modelSource,
                        mountPath: modelSource === 'huggingface' ? '' : modelPath.trim(),
                        // The actual Deploy providers resolve this into a PVC mount
                        // (any storage kind) via storage_mount.py::resolve_mount(),
                        // taking priority over the hostPath-only mountPath above.
                        storageVolumeId: modelSource === 'auto-cache' ? modelCacheVolumeId : '',
                    },
                };
                if (['pd-disaggregation', 'tiered-prefix-cache'].includes(workload.guide)) candidateConfig.guide_variant = guideVariant;
                setProgress(`Rendering artifact · configuration ${index + 1} of ${variants.length}`);
                const rendered = await renderConfiguration(candidateConfig, {
                    guide_ref: workload.guide,
                    template_ref: variantPlan.source?.files?.[0],
                    guide_source: variantPlan.source,
                    rendered_manifest: variantPlan.plannedDeployment?.content,
                    deployment_bundle: variantPlan.deploymentBundle,
                    cluster_ref: {
                        id: targetCluster.serverId,
                        name: targetCluster.name,
                        session_id: targetCluster.sessionId,
                        connection: targetCluster.transport,
                    },
                    deployment: variantPlan.deployment,
                    deploymentName: deploymentName.trim() || undefined,
                    modelSecret: {
                        mode: 'none',
                    },
                });
                setProgress(`Saving configuration ${index + 1} of ${variants.length}`);
                const saved = await saveConfiguration(
                    { ...rendered.deployable_configuration, provenance: {
                        ...rendered.deployable_configuration.provenance,
                        editor_source: { guideSourceMode, localGuidePath, remoteGuideRepository, remoteGuideRef, remoteGuidePath },
                        configuration_validation: variantPlan.validation,
                    } },
                    `${slug(workload.guide)}-${isPd ? `p${variant.prefillReplicaCount}d${variant.replicaCount}-ptp${variant.prefillTpCount}-dtp${variant.tpCount}` : `r${variant.replicaCount}-tp${variant.tpCount}`}-${Date.now().toString(36)}.yaml`,
                );
                savedArtifacts.push(saved.artifact);
                plans.push({
                    id: `${slug(workload.guide)}-${isPd ? `p${variant.prefillReplicaCount}d${variant.replicaCount}-ptp${variant.prefillTpCount}-dtp${variant.tpCount}` : `r${variant.replicaCount}-tp${variant.tpCount}`}-${index + 1}`,
                    configuration_artifact_id: saved.artifact.artifact_id,
                    include_baseline: launchBaselineTypes.length > 0,
                    baseline_type: launchBaselineTypes[0] || 'direct-vllm',
                    baseline_types: launchBaselineTypes,
                    preserve_deployment: false,
                    benchmark: workload.benchmark,
                });
            }
            if (onPublished) onPublished({ artifacts: savedArtifacts, advance, optimizationSelection });
            else sendWorkloadsToEvaluate(plans, savedArtifacts[0], onNavigate);
        } catch (error) {
            const globalErrors = showServerErrors([error.message || 'Could not publish configuration']);
            setPublishError(globalErrors.join(' '));
        } finally {
            setPublishing(false);
        }
    };

    const uploadConfiguration = async (event) => {
        const file = event.target.files?.[0];
        event.target.value = '';
        if (!file) return;
        setUploadingConfiguration(true);
        setUploadError('');
        setProgress('Importing configuration YAML…');
        try {
            const renderedManifest = await file.text();
            validateImportManifest(renderedManifest);
            if (configurationInputErrors.length) throw new Error(configurationInputErrors.map(error => error.message).join(' '));
            const variants = expandConfigurationTopologies(topologyInputs);
            if (variants.length !== 1) throw new Error('Select exactly one topology before importing annotated YAML.');
            const variant = variants[0];
            if (modelSource === 'auto-cache' && modelCacheValidation.state !== 'ready') {
                throw new Error(modelCacheValidation.message || 'Select a Model Cache storage volume that has this model ready before uploading a configuration');
            }
            const targetCluster = await requireSelectedCluster();
            const isPd = selection.guide === 'pd-disaggregation';
            const uploadPlan = await planGuideDeployment(
                planningRequest(targetCluster, variant.replicaCount, variant.tpCount, variant.prefillReplicaCount, variant.prefillTpCount),
            );
            if (uploadPlan.validation?.status === 'invalid' || uploadPlan.validation?.errors?.length) {
                throw new Error((uploadPlan.validation.errors || []).join('; '));
            }
            const candidateConfig = {
                schema_version: '1.0',
                type: isPd ? 'pd' : 'baseline',
                guide_settings: normalizeGuideSettings(selection.guide, guideVariant, guideSettings),
                candidate_source: { name: 'manual' },
                guide_variant: guideVariant,
                custom_parameters: [
                    ...runtimeControlOverrides({ maxModelLen, maxNumSeqs, gpuMemoryUtilization, blockSize, maxNumBatchedTokens }, selection.modelServer),
                    ...customRows,
                ],
                target: { model: model.trim(), cluster_id: targetCluster.serverId, cluster_name: targetCluster.name },
                serving: { replicas: variant.replicaCount, tensor_parallel_size: variant.tpCount, max_model_len: (selection.modelServer === 'vllm' && maxModelLen !== '' ? Number(maxModelLen) : undefined) },
                prefill: isPd ? { replicas: variant.prefillReplicaCount, tensor_parallel_size: variant.prefillTpCount } : null,
                decode: { replicas: variant.replicaCount, tensor_parallel_size: variant.tpCount, max_model_len: (selection.modelServer === 'vllm' && maxModelLen !== '' ? Number(maxModelLen) : undefined), max_num_seqs: (selection.modelServer === 'vllm' && maxNumSeqs !== '' ? Number(maxNumSeqs) : undefined) },
                runtime: {
                    image, imageMode, buildSourceUrl, modelSource, modelServer: selection.modelServer,
                    modelPvcClaimName: selectedModelCacheVolume?.pvcName || '',
                    mountPath: modelSource === 'huggingface' ? '' : modelPath.trim(),
                    storageVolumeId: modelSource === 'auto-cache' ? modelCacheVolumeId : '',
                },
            };
            const rendered = await renderConfiguration(candidateConfig, {
                guide_ref: selection.guide,
                template_ref: `upload/${file.name}`,
                guide_source: { ...uploadPlan.source, files: [file.name] },
                rendered_manifest: renderedManifest,
                deployment_bundle: uploadPlan.deploymentBundle,
                reference_manifest: uploadPlan.plannedDeployment.content,
                cluster_ref: { id: targetCluster.serverId, name: targetCluster.name, session_id: targetCluster.sessionId, connection: targetCluster.transport },
                deployment: uploadPlan.deployment,
                modelSecret: { mode: 'none' },
            });
            const saved = await saveConfiguration({ ...rendered.deployable_configuration, provenance: {
                ...rendered.deployable_configuration.provenance,
                editor_source: { guideSourceMode, localGuidePath, remoteGuideRepository, remoteGuideRef, remoteGuidePath },
                configuration_validation: uploadPlan.validation,
            } }, file.name);
            const plans = [{
                id: `${slug(selection.guide)}-uploaded`,
                configuration_artifact_id: saved.artifact.artifact_id,
                include_baseline: launchBaselineTypes.length > 0,
                baseline_type: launchBaselineTypes[0] || 'direct-vllm',
                baseline_types: launchBaselineTypes,
                preserve_deployment: false,
                benchmark: defaultBenchmark(selection.guide),
            }];
            if (onPublished) onPublished({ artifacts: [saved.artifact], optimizationSelection });
            else sendWorkloadsToEvaluate(plans, saved.artifact, onNavigate);
        } catch (error) {
            setUploadError(error.message || 'Could not import the configuration YAML');
        } finally {
            setUploadingConfiguration(false);
        }
    };

    const sendGuideToEvaluate = (advance = false) => {
        const isPd = selection.guide === 'pd-disaggregation';
        const reps = Number(replicas);
        const tp = Number(tensorParallelSize);
        const workload = {
            id: `${slug(selection.guide)}-${Date.now().toString(36)}`,
            guide: selection.guide,
            guide_variant: ['pd-disaggregation', 'tiered-prefix-cache'].includes(selection.guide) ? guideVariant : '',
            model: model.trim(),
            mount_path: '',
            replicas: reps,
            tensor_parallel_size: tp,
            prefill_replicas: isPd ? Number(prefillReplicas) : null,
            prefill_tensor_parallel_size: isPd ? Number(prefillTensorParallelSize) : null,
            decode_replicas: isPd ? reps : null,
            decode_tensor_parallel_size: isPd ? tp : null,
            include_baseline: true,
            custom_parameters: customRows,
            benchmark: defaultBenchmark(selection.guide),
        };
        publishWorkload(workload, 'manual', advance);
    };

    const usePdReportPreset = () => {
        // Apply the report's topology ratios to the currently selected model.
        const tp = liveModelAdvice?.recommendedTp || Number(tensorParallelSize) || 1;
        setPrefillReplicas(1);
        setReplicas(1);
        setPrefillTensorParallelSize(tp);
        setTensorParallelSize(tp);
        // Preserve the report's four P:D ratios; capacity validation checks each one.
        setPdTopologyVariants('1:1,2:2,1:3,3:1');
        setPrefillReplicaVariants('1');
        setReplicaVariants('1');
        setPrefillTensorParallelVariants(String(tp));
        setTensorParallelVariants(String(tp));
        setPlanResult(null);
    };

    const runAic = async (pdOnly = false) => {
        const availableGpus = budget;
        if (!connectedCluster || clusterHardwareLoading) {
            setAicError('Wait for the selected cluster capacity check to finish.');
            return;
        }
        if (availableGpus < 1) {
            setAicError('The selected cluster has no available XPU cards for a new topology.');
            return;
        }
        setAicLoading(true);
        setAicError('');
        setAicCandidates(null);
        const workload = { model: model.trim(), isl: 1024, osl: 256 };
        const searchConfig = {
            aicSystemName: aicSystem.trim(),
            aicBackendName: aicBackend,
            aicDatabaseMode: 'SILICON',
            totalGpus: availableGpus,
        };
        try {
            const support = await checkAicSupport(workload, searchConfig);
            if ((pdOnly || pdRecommendation || selection.guide === 'pd-disaggregation') && support.disagg_supported === false) {
                throw new Error(support.reason || `AIC does not support P/D serving for ${model.trim()} on ${aicSystem}.`);
            }
            const candidates = await searchAicCandidates(workload, ['aic'], searchConfig, null);
            const requirePd = pdOnly || pdRecommendation || selection.guide === 'pd-disaggregation';
            const matchingCandidates = requirePd ? candidates.filter((candidate) => candidate.topologyMode === 'disagg') : candidates;
            if (requirePd && candidates.length && !matchingCandidates.length) {
                const minimum = liveModelAdvice?.recommendedTp ? liveModelAdvice.recommendedTp * 2 : null;
                throw new Error(`AIC found only aggregated topologies within the ${availableGpus}-accelerator budget.${minimum ? ` This model needs at least approximately ${minimum} accelerators for one prefill and one decode replica at TP ${liveModelAdvice.recommendedTp}.` : ''}`);
            }
            setAicCandidates(matchingCandidates);
        } catch (error) {
            setAicError(error.message || 'AIC prediction failed');
        } finally {
            setAicLoading(false);
        }
    };

    const applyAicCandidate = (candidate) => {
        const isPd = candidate.topologyMode === 'disagg' || Boolean(candidate.prefillTp || candidate.prefillReplicas);
        const guideId = isPd ? 'pd-disaggregation' : 'optimized-baseline';
        const guide = guides.find((item) => item.id === guideId && item.deploymentCapability);
        const preferredAccelerator = /bmg|max_|xpu|b60|pvc/i.test(aicSystem) ? 'xpu' : 'gpu';
        const accelerator = guide?.accelerators?.find((item) => item.id === preferredAccelerator) || guide?.accelerators?.[0];
        const modelServer = accelerator?.modelServers?.find((item) => item.id === aicBackend) || accelerator?.modelServers?.[0];
        if (!guide || !accelerator || !modelServer) {
            setAicError(`No deployable official ${guideId} Guide matches ${preferredAccelerator}/${aicBackend}`);
            return;
        }
        setSelection({ guide: guide.id, accelerator: accelerator.id, modelServer: sharedContext?.modelServer?.toLowerCase() || modelServer.id });
        setPrefillReplicas(isPd ? candidate.prefillReplicas || 1 : 1);
        setPrefillTensorParallelSize(isPd ? candidate.prefillTp || 1 : 1);
        setReplicas(candidate.decodeReplicas || 1);
        setTensorParallelSize(candidate.decodeTp || 1);
        // Explicit P:D pairs otherwise override the recommended replica counts.
        setPdTopologyVariants('');
        setReplicaVariants(String(candidate.decodeReplicas || 1));
        setTensorParallelVariants(String(candidate.decodeTp || 1));
        setAicCandidates(null);
        setPlanResult(null);
        setPlanError(null);
    };

    const summary = planResult?.summary || {};
    const validation = planResult?.validation || {};

    return (
        <ConfigurationValidationContext.Provider value={{ errors: fieldErrors, submitted: validationSubmitted }}>
        <section ref={editorRef} className={`configuration-editor mx-auto flex max-w-[1500px] flex-col gap-4 text-slate-100 ${embedded ? 'py-2' : 'px-5 py-7'}`}>
            {embedded && validationSubmitted && fieldErrors.some(error => error.field === 'setup') && <div data-configuration-field="setup" tabIndex={-1}>
                {validationSubmitted && fieldErrors.filter(error => error.field === 'setup').map(error => <p key={error.message} className="text-xs text-rose-300">{error.message}</p>)}
            </div>}
            <header className="flex flex-wrap items-end justify-between gap-4 border-b border-slate-800 pb-5">
                <div>
                    <p className="text-xs font-semibold uppercase text-emerald-400">Optimization</p>
                    <h1 className="mt-1 text-2xl font-semibold">{embedded ? "Configuration editor" : "Configuration"}</h1>
                    <p className="mt-2 text-sm text-slate-400">
                        Build a deployable configuration from an llm-d guide template, or let AIC predict a topology,{' '}
                        {embedded ? 'then save it to this task.' : 'then send the parameters to Evaluate.'}
                    </p>
                </div>
                 {modelMarketDraft ? <button type="button" onClick={() => { sessionStorage.setItem(MODEL_MARKET_DRAFT_KEY, JSON.stringify(modelMarketDraft)); sessionStorage.setItem('prism_model_market_return_draft', JSON.stringify({ model: modelMarketDraft.model })); sessionStorage.setItem('prism_model_market_return_draft_values', JSON.stringify(modelMarketDraft)); onNavigate('model-market'); }} className="inline-flex items-center gap-2 border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:border-slate-500 hover:text-white"><ArrowLeft className="h-3.5 w-3.5" />Back to Model Cards</button> : <button type="button" disabled={operationBusy} onClick={returnFromConfiguration} aria-label="Close configuration" title="Close" className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-slate-700 text-slate-400 hover:border-slate-500 hover:bg-slate-800 hover:text-white">
                     <X className="h-4 w-4" />
                 </button>}
            </header>

            {publishError && (
                <div className="flex items-center gap-2 border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">
                    <AlertTriangle className="h-4 w-4" /> {publishError}
                </div>
            )}

            {!singleConfiguration && <section data-configuration-field="cluster" tabIndex={-1} id="configuration-cluster" className="border border-slate-800 bg-slate-950/40 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                        <h2 className="text-sm font-semibold">Target cluster</h2>
                        <p className="mt-1 text-[11px] text-slate-500">Created clusters are loaded from the Cluster API. Selecting one activates its deployment session.</p>
                    </div>
                    <div className="flex gap-2">
                        <button type="button" onClick={() => setClusterReloadToken((value) => value + 1)} disabled={clustersLoading} className="border border-slate-700 px-3 py-1.5 text-xs text-slate-300 disabled:opacity-50">Refresh</button>
                        <button type="button" onClick={() => onNavigate('clusters')} className="border border-slate-700 px-3 py-1.5 text-xs text-slate-300">Manage clusters</button>
                    </div>
                </div>
                {clustersLoading ? (
                    <div className="mt-3 flex items-center gap-2 border border-slate-800 p-3 text-xs text-slate-400">
                        <Loader2 className="h-4 w-4 animate-spin" /> Loading created clusters…
                    </div>
                ) : (
                    <label className="mt-3 block">
                        <span className={labelClass}>Created cluster</span>
                        <select
                            className={inputClass}
                            value={selectedClusterId}
                            onChange={(event) => {
                                setSelectedClusterId(event.target.value);
                                setPlanResult(null);
                                setPlanError('');
                            }}
                        >
                            <option value="">Select a ready cluster</option>
                            {clusters.map((cluster) => (
                                <option key={cluster.id} value={cluster.id} disabled={!cluster.ready}>
                                    {cluster.name} ({cluster.id}){cluster.ready ? '' : ' — unavailable'}
                                </option>
                            ))}
                        </select>
                    </label>
                )}
                {clusterError && (
                    <div className="mt-3 border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-200">{clusterError}</div>
                )}
                {!clustersLoading && !clusterError && clusters.length === 0 && (
                    <div className="mt-3 border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-100">No created clusters are available. Create one in Cluster first.</div>
                )}
                {clusterSessionLoading && (
                    <div className="mt-3 flex items-center gap-2 border border-cyan-500/20 bg-cyan-500/5 p-3 text-xs text-cyan-200">
                        <Loader2 className="h-4 w-4 animate-spin" /> Activating cluster session…
                    </div>
                )}
                {connectedCluster && !clusterSessionLoading && (
                    <div className="mt-3 border border-emerald-500/30 bg-emerald-500/10 p-3">
                        <p className="text-xs font-semibold text-emerald-200">{connectedCluster.name} ready</p>
                        <p className="mt-1 font-mono text-[10px] text-emerald-300/80">{connectedCluster.serverId} · session {connectedCluster.sessionId}</p>
                    </div>
                )}
            </section>}

            {!singleConfiguration && <div id="configuration-model" className="grid gap-3 border border-slate-800 bg-slate-950/40 p-4 sm:grid-cols-2">
                <div className="sm:col-span-2">
                    <span className={labelClass}>Model source</span>
                    <select className={inputClass} value={modelSource} onChange={(event) => setModelSource(event.target.value)}>
                        <option value="auto-cache">Shared Hugging Face cache first; download if missing</option>
                        <option value="shared-path">Exact model folder on Kubernetes worker nodes</option>
                        <option value="huggingface">Always resolve from Hugging Face</option>
                    </select>
                </div>
                <ConfigurationField field="model" className="sm:col-span-2">
                    <span className={labelClass}>{modelSource === 'shared-path' ? 'Served model name' : 'Model ID'}</span>
                    <input className={inputClass} value={model} placeholder="Organization/model-name"
                        onChange={(event) => setModel(event.target.value)} />
                </ConfigurationField>
                <div className="sm:col-span-2">
                    <span className={labelClass}>Deployment name</span>
                    <input className={inputClass} value={deploymentName} placeholder="Optional display name"
                        onChange={(event) => setDeploymentName(event.target.value)} />
                </div>
                {modelSource === 'shared-path' && <ConfigurationField field="modelPath" className="sm:col-span-2">
                    <span className={labelClass}>Exact shared model folder</span>
                    <input className={inputClass} value={modelPath} placeholder="/shared/models/Qwen3-32B" onChange={(event) => setModelPath(event.target.value)} />
                    <p className="mt-1 text-[10px] text-amber-300">This absolute path must contain a complete model on every eligible worker node. It is mounted read-only at /model-cache; no download is performed.</p>
                </ConfigurationField>}
                {modelSource === 'auto-cache' && <div className="sm:col-span-2">
                    <span data-configuration-field="modelCacheVolumeId" tabIndex={-1} className={labelClass}>Model cache storage</span>
                    <StorageVolumeSelect
                        className="mt-1"
                        value={modelCacheVolumeId}
                        onChange={setModelCacheVolumeId}
                        volumes={modelCacheVolumes}
                        loading={modelCacheVolumesLoading}
                        disabled={!modelCacheVolumes.length}
                        placeholder="Select Model Cache storage"
                        emptyLabel={modelCacheVolumesLoading ? 'Loading storage volumes...' : 'No Model Cache storage volume registered for this cluster'}
                        optionDetail={(volume) => `${volume.kind}: ${volume.localDisk?.hostPath || (volume.nfs ? `${volume.nfs.server}:${volume.nfs.path}` : volume.dynamicPvc ? volume.dynamicPvc.storageClass : volume.id)}`}
                    />
                    {modelCacheVolumesError && <p className="mt-1 text-[10px] text-rose-400">{modelCacheVolumesError}</p>}
                    {modelCacheEntriesError && <p className="mt-1 text-[10px] text-rose-400">{modelCacheEntriesError}</p>}
                    {modelCacheValidation.state === 'loading' && (
                        <p className="mt-1 text-[10px] text-slate-500">Checking whether this model is cached…</p>
                    )}
                    {modelCacheValidation.state === 'missing' && (
                        <p className="mt-1 text-[10px] text-rose-400">
                            {modelCacheValidation.message} <button type="button" className="underline" onClick={() => onNavigate?.('model-cache')}>Open Model Cache</button>
                        </p>
                    )}
                    {modelCacheValidation.state === 'no-volume' && !modelCacheVolumesLoading && (
                        <p className="mt-1 text-[10px] text-rose-400">{modelCacheValidation.message}</p>
                    )}
                </div>}
                <div>
                    <span className={labelClass}>Image mode</span>
                    <select className={inputClass} value={imageMode} onChange={(event) => setImageMode(event.target.value)}>
                        <option value="use-upstream-image">Use image</option>
                        <option value="build-from-source">Build source</option>
                    </select>
                </div>
                <div>
                    <span className={labelClass}>{imageMode === 'build-from-source' ? 'Build image name' : 'Runtime image'}</span>
                    <input className={inputClass} value={image} onChange={(event) => setImage(event.target.value)} />
                </div>
                {imageMode === 'build-from-source' && <div className="sm:col-span-2">
                    <span className={labelClass}>Build source URL</span>
                    <input className={inputClass} value={buildSourceUrl} onChange={(event) => setBuildSourceUrl(event.target.value)} />
                </div>}
                {modelSource !== 'shared-path' && <p className="sm:col-span-2 text-[10px] text-slate-500">Public models need no credential. If a private or gated model cannot be downloaded, enter its Hugging Face token on the failed deployment and retry.</p>}
            </div>}

            <div className="space-y-4">
                    <section className="configuration-source rounded-xl border border-slate-800 bg-slate-950/40 p-5">
                        <div className="mb-4 flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 pb-4">
                            <div><h2 className="text-sm font-semibold">Configuration source</h2><p className="mt-1 text-[10px] text-slate-500">Choose a Guide or import YAML.</p></div>
                            <label className={`inline-flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold transition ${uploadingConfiguration ? 'pointer-events-none border-slate-700 text-slate-500' : 'border-cyan-500/40 bg-cyan-500/10 text-cyan-200 hover:border-cyan-400 hover:bg-cyan-500/15'}`}>
                                {uploadingConfiguration ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}{uploadingConfiguration ? 'Importing…' : 'Import YAML'}
                                <input type="file" accept=".yaml,.yml,text/yaml,application/yaml" className="hidden" onChange={uploadConfiguration} disabled={operationBusy} />
                            </label>
                        </div>
                        {uploadError && <div role="alert" className="mb-3 flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-200"><AlertTriangle className="h-4 w-4 shrink-0" />{uploadError}</div>}
                        <fieldset disabled={operationBusy} className="min-w-0 border-0 p-0">
                        {singleConfiguration && <div className="mb-5 border-b border-slate-800 pb-5">
                            <section>
                                <div className="mb-4 border-l-2 border-emerald-400/70 bg-emerald-500/5 px-3 py-2">
                                    <h3 className="text-xs font-semibold uppercase tracking-wider text-emerald-300">Basic settings</h3>
                                </div>
                                <div className="grid gap-3 md:grid-cols-3">
                                    <ConfigurationField field="model"><span className={labelClass}>Model</span><input className={inputClass} value={model} disabled /></ConfigurationField>
                                    <ConfigurationField field="cluster"><span className={labelClass}>Cluster</span><input className={inputClass} value={connectedCluster?.name || connectedCluster?.serverId || ''} disabled /></ConfigurationField>
                                    <ConfigurationField field="modelServer"><span className={labelClass}>Runtime</span><input className={inputClass} value={selection.modelServer} disabled /></ConfigurationField>
                                </div>
                                <div className="mt-3 grid gap-3 md:grid-cols-3">
                                    <ConfigurationField field="image"><span className={labelClass}>Runtime image</span><input className={inputClass} list="runtime-image-suggestions" value={image} onChange={(event) => setImage(event.target.value)} /><datalist id="runtime-image-suggestions">{[...new Set([DEFAULT_RUNTIME_IMAGE, ...(sharedContext?.cachedRuntimeImages || [])])].map((cachedImage) => <option key={cachedImage} value={cachedImage} />)}</datalist></ConfigurationField>
                                    <ConfigurationField field="replicaVariants"><span className={labelClass}>{selection.guide === 'pd-disaggregation' ? 'Decode replicas' : 'Replicas'}</span><input type="number" min="1" step="1" className={inputClass} value={replicas} onChange={(event) => setReplicas(event.target.value)} /></ConfigurationField>
                                    <ConfigurationField field="tensorParallelVariants"><span className={labelClass}>{selection.guide === 'pd-disaggregation' ? 'Decode TP' : 'TP'}</span><input type="number" min="1" step="1" className={inputClass} value={tensorParallelSize} onChange={(event) => setTensorParallelSize(event.target.value)} /></ConfigurationField>
                                    {selection.guide === 'pd-disaggregation' && <><ConfigurationField field="prefillReplicaVariants"><span className={labelClass}>Prefill replicas</span><input type="number" min="1" step="1" className={inputClass} value={prefillReplicas} onChange={(event) => setPrefillReplicas(event.target.value)} /></ConfigurationField><ConfigurationField field="prefillTensorParallelVariants"><span className={labelClass}>Prefill TP</span><input type="number" min="1" step="1" className={inputClass} value={prefillTensorParallelSize} onChange={(event) => setPrefillTensorParallelSize(event.target.value)} /></ConfigurationField></>}
                                </div>
                                <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
                                    <span>Accelerators: {topologyPreviewVariants.length ? Math.max(...topologyPreviewVariants.map(row => row.gpuCount)) : '—'} / {budget}</span>
                                    {liveModelAdvice ? <><span className={topologyPreviewVariants.some(row => row.tpCount < liveModelAdvice.recommendedTp || (selection.guide === 'pd-disaggregation' && row.prefillTpCount < liveModelAdvice.recommendedTp)) ? 'text-amber-300' : ''}>Estimated TP ≥ {liveModelAdvice.recommendedTp}</span><FieldHelp>{liveModelAdvice.parametersBillions}B model ≈ {liveModelAdvice.estimatedWeightMemoryGiB} GiB including a 20% margin, assuming BF16/FP16 and {liveModelAdvice.acceleratorMemoryGiB} GiB per card. This is an estimate from the model name. Generate checks the Guide and cluster.</FieldHelp></> : <span>Model capacity estimate unavailable</span>}
                                </div>
                            </section>
                        </div>}
                        <div className="mb-4 border-l-2 border-emerald-400/70 bg-emerald-500/5 px-3 py-2">
                            <h3 className="text-xs font-semibold uppercase tracking-wider text-emerald-300">Guide template</h3>
                        </div>
                        {catalogError && (
                            <div className="mb-3 flex items-center gap-2 border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-200">
                                <AlertTriangle className="h-4 w-4" /> {catalogError}
                            </div>
                        )}
                        {catalogLoading ? (
                            <div className="flex items-center gap-2 text-xs text-slate-400">
                                <Loader2 className="h-4 w-4 animate-spin" /> Loading guide catalog…
                            </div>
                        ) : (
                            <div className={`grid gap-3 ${singleConfiguration ? 'md:grid-cols-2' : 'md:grid-cols-3'}`}>
                                <ConfigurationField field="guide">
                                    <span className={labelClass}>Guide</span>
                                    <select className={inputClass} value={selection.guide} onChange={(event) => selectGuide(event.target.value)}>
                                        {guides.map((guide) => <option key={guide.id} value={guide.id} disabled={!guide.deploymentCapability}>{guide.label || guide.id}{guide.deploymentCapability ? '' : ' (not deployable)'}</option>)}
                                    </select>
                                    {selectedGuide && !selectedGuide.deploymentCapability && <p className="mt-1 text-[10px] text-amber-300">No end-to-end Deploy provider is registered.</p>}
                                </ConfigurationField>
                                <ConfigurationField field="accelerator">
                                    <span className={labelClass}>Accelerator</span>
                                    <select className={inputClass} value={selection.accelerator} onChange={(event) => selectAccelerator(event.target.value)}>
                                        {accelerators.map((accelerator) => <option key={accelerator.id} value={accelerator.id}>{accelerator.id}</option>)}
                                    </select>
                                </ConfigurationField>
                                {!singleConfiguration && <ConfigurationField field="modelServer">
                                    <span className={labelClass}>{singleConfiguration ? 'Runtime' : 'Model server'}</span>
                                    <select disabled={Boolean(sharedContext)} className={inputClass} value={selection.modelServer} onChange={(event) => setSelection((current) => ({ ...current, modelServer: event.target.value }))}>
                                        {modelServers.map((server) => <option key={server.id} value={server.id}>{server.id}</option>)}
                                    </select>
                                </ConfigurationField>}
                            </div>
                        )}
                        {selection.guide === 'pd-disaggregation' && <div className="mt-3 grid gap-3 md:grid-cols-2">
                                <ConfigurationField field="guideVariant"><span className={labelClass}>P/D Guide variant</span><select className={inputClass} value={guideVariant} onChange={(event) => { setGuideVariant(event.target.value); setGuideSettings(current => ({ routerValues: current.routerValues || '' })); }}>{guideVariants.map((variant) => <option key={variant} value={variant}>{variant === 'vllm-rdma' ? 'vLLM + RDMA overlay' : variant === 'base' ? 'vLLM (base)' : variant === 'vllm' ? 'vLLM (XPU)' : variant}</option>)}{unavailableGuideVariants.map((item) => <option key={item.id} value={item.id} disabled>{`${item.label} (unavailable)`}</option>)}</select><FieldHelp>Set NIC count in Cache & network. Network selectors and alignment constraints come from the selected Guide.</FieldHelp></ConfigurationField>
                            </div>}
                        {selection.guide === 'tiered-prefix-cache' && <div className="mt-3 grid gap-3 md:grid-cols-2">
                                <ConfigurationField field="guideVariant"><span className={labelClass}>Tiered-prefix-cache variant</span><select className={inputClass} value={guideVariant} onChange={(event) => { setGuideVariant(event.target.value); setGuideSettings(current => ({ routerValues: current.routerValues || '' })); }}>{guideVariants.map((variant) => <option key={variant} value={variant}>{variant === 'base' ? 'HBM-only baseline' : variant === 'native/cpu/base' ? 'Native CPU offload · capacity from Guide' : variant === 'lmcache-connector/cpu/base' ? 'LMCache CPU offload · capacity from Guide' : variant}</option>)}</select><FieldHelp>Create separate base and offload artifacts, then select both in one Evaluation for a direct pairwise comparison.</FieldHelp></ConfigurationField>

                            </div>}
                        <div className="mt-3 grid gap-3 md:grid-cols-3">
                            <ConfigurationField field="guideSourceMode">
                                <span className={labelClass}>Reference source</span>
                                <select className={inputClass} value={guideSourceMode} onChange={(event) => { setGuideSourceMode(event.target.value); setPlanResult(null); }}>
                                    <option value="official">Official llm-d repository</option>
                                    <option value="remote">Remote GitHub repository</option>
                                    <option value="local">Local YAML / Kustomize path</option>
                                </select>
                            </ConfigurationField>
                            {guideSourceMode === 'local' && <ConfigurationField field="localGuidePath" className="md:col-span-2">
                                <span className={labelClass}>Local YAML or Kustomize path</span>
                                <input className={inputClass} value={localGuidePath} placeholder="/path/to/llm-d/guides/.../kustomization.yaml"
                                    onChange={(event) => { setLocalGuidePath(event.target.value); setPlanResult(null); }} />
                                <p className="mt-1 text-[10px] text-slate-500">The server only accepts paths under configured allowlisted roots.</p>
                            </ConfigurationField>}
                            {guideSourceMode === 'remote' && <>
                                <ConfigurationField field="remoteGuideRepository">
                                    <span className={labelClass}>GitHub repository</span>
                                    <input className={inputClass} value={remoteGuideRepository} placeholder="owner/repository"
                                        onChange={(event) => { setRemoteGuideRepository(event.target.value); setPlanResult(null); }} />
                                </ConfigurationField>
                                <ConfigurationField field="remoteGuideRef">
                                    <span className={labelClass}>Branch / tag / commit</span>
                                    <input className={inputClass} value={remoteGuideRef} placeholder="main"
                                        onChange={(event) => { setRemoteGuideRef(event.target.value); setPlanResult(null); }} />
                                </ConfigurationField>
                                <ConfigurationField field="remoteGuidePath" className="md:col-span-3">
                                    <span className={labelClass}>YAML or Kustomization path in repository</span>
                                    <input className={inputClass} value={remoteGuidePath} placeholder="guides/optimized-baseline/modelserver/xpu/vllm/kustomization.yaml"
                                        onChange={(event) => { setRemoteGuidePath(event.target.value); setPlanResult(null); }} />
                                </ConfigurationField>
                            </>}
                        </div>
                        <p className="mt-3 break-all border-t border-slate-800 pt-3 font-mono text-xs leading-5 text-slate-500">Source: {guideSourceSummary}</p>

                        {!singleConfiguration && <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
                            <span>Accelerators: {topologyPreviewVariants.length ? Math.max(...topologyPreviewVariants.map(row => row.gpuCount)) : '—'} / {budget}{topologyPreviewVariants.length > 1 ? ' max per configuration' : ''}</span>
                            {liveModelAdvice ? <>
                                <span className={topologyPreviewVariants.some(row => row.tpCount < liveModelAdvice.recommendedTp || (selection.guide === 'pd-disaggregation' && row.prefillTpCount < liveModelAdvice.recommendedTp)) ? 'text-amber-300' : ''}>Estimated TP ≥ {liveModelAdvice.recommendedTp}</span>
                                <FieldHelp>{liveModelAdvice.parametersBillions}B model ≈ {liveModelAdvice.estimatedWeightMemoryGiB} GiB including a 20% margin, assuming BF16/FP16 and {liveModelAdvice.acceleratorMemoryGiB} GiB per card. This is an estimate from the model name. Generate checks the Guide and cluster.</FieldHelp>

                            </> : <span>Model capacity estimate unavailable</span>}
                        </div>}
                        {selection.guide === 'pd-disaggregation' && <section className="mt-4 rounded-lg border border-slate-800 p-3"><h3 className="mb-3 text-sm font-medium text-violet-200">Recommended topology</h3>
                            <div className="flex items-center justify-between gap-3"><div><p className="text-xs text-violet-200">AIC P/D recommendation</p><p className="mt-1 text-[10px] text-violet-300">{clusterHardwareLoading ? 'Checking cluster XPU capacity…' : clusterHardware ? `${clusterHardware.usableGpuCount ?? clusterHardware.availableGpuCount} currently available · ${clusterHardware.configuredGpuLimit != null ? `${clusterHardware.configuredGpuLimit} in configured scope` : 'no device limit configured'} · ${clusterHardware.gpuCount} physical total` : 'Select a ready cluster to check XPU capacity.'}</p></div><button type="button" disabled={aicLoading || clusterHardwareLoading || !connectedCluster || budget < 1 || !model.trim()} onClick={() => { setPdRecommendation(true); runAic(true); }} className="inline-flex items-center gap-2 border border-violet-500 px-3 py-2 text-xs text-violet-200 disabled:opacity-40">{aicLoading || clusterHardwareLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <WandSparkles className="h-4 w-4" />}Use AIC for PD</button></div>
                            {aicError && <div className="mt-3 border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-200">{aicError}</div>}
                            {aicCandidates && <div className="mt-3 space-y-2">
                                {aicCandidates.length === 0 && <p className="text-xs text-slate-500">AIC returned no deployable P/D candidates.</p>}
                                {aicCandidates.map((candidate) => <div key={candidate.id} className="flex flex-wrap items-center gap-3 border border-slate-800 bg-slate-950/60 p-3 text-xs">
                                    <span className="font-mono text-slate-200">{candidate.name || candidate.id}</span>
                                    <span className="font-mono text-slate-400">P {candidate.prefillTp || 1}×{candidate.prefillReplicas || 1}</span>
                                    <span className="font-mono text-slate-400">D {candidate.decodeTp || 1}×{candidate.decodeReplicas || 1}</span>
                                    {candidate.predicted && <span className="text-slate-500">TTFT {candidate.predicted.ttftMs}ms · {candidate.predicted.throughputTps} tok/s</span>}
                                    <button type="button" disabled={candidate.deployable === false} onClick={() => applyAicCandidate(candidate)} className="ml-auto border border-cyan-500 px-2 py-1.5 text-cyan-200 disabled:opacity-40">Use this topology</button>
                                </div>)}
                            </div>}
                        </section>}
                        {!singleConfiguration && <fieldset className="configuration-comparison mt-3 min-w-0 rounded-lg border border-slate-800 p-4">
                            <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-xs text-slate-400">Enter one value, or comma-separated values to compare up to 16 configurations.</p>{selection.guide === 'pd-disaggregation' && <button type="button" onClick={usePdReportPreset} className="text-xs text-violet-300 underline">Apply P:D ratios (1:1, 2:2, 1:3, 3:1)</button>}</div>
                            <div className="mt-3 grid gap-3 md:grid-cols-2">
                                {selection.guide === 'pd-disaggregation' && <><ConfigurationField field="pdTopologyVariants" className="md:col-span-2"><span className={labelClass}>P:D replica pairs</span><input className={inputClass} value={pdTopologyVariants} placeholder="1:1,2:2,1:3,3:1" onChange={(event) => setPdTopologyVariants(event.target.value)} /><FieldHelp>Use P:D replica pairs for an exact topology suite. The whitepaper preset generates 1P1D, 2P2D, 1P3D and 3P1D.</FieldHelp></ConfigurationField><ConfigurationField field="prefillReplicaVariants"><span className={labelClass}>Prefill replicas</span><input className={inputClass} value={prefillReplicaVariants} disabled={Boolean(pdTopologyVariants.trim())} placeholder="1,2,3" onChange={(event) => setPrefillReplicaVariants(event.target.value)} /><span className="mt-2 flex items-center gap-2"><span className="text-[10px] text-slate-500">Quick select</span>{['1', '1,2', '1,2,3'].map(value => <button key={value} type="button" disabled={selection.guide === 'pd-disaggregation' && Boolean(pdTopologyVariants.trim())} onClick={() => setPrefillReplicaVariants(value)} aria-pressed={prefillReplicaVariants === value} className={`rounded-md border px-2.5 py-1.5 text-[10px] transition disabled:opacity-40 ${prefillReplicaVariants === value ? 'border-cyan-400/60 bg-cyan-400/10 text-cyan-200' : 'border-slate-700 text-slate-400 hover:border-cyan-500/50 hover:text-cyan-200'}`}>{value}</button>)}</span></ConfigurationField>
<ConfigurationField field="prefillTensorParallelVariants" suggestion={liveModelAdvice && String(prefillTensorParallelVariants).split(',').some(value => Number(value) < liveModelAdvice.recommendedTp) && <button type="button" className="mt-1 block text-xs text-amber-300 underline" onClick={() => { setPrefillTensorParallelVariants(String(liveModelAdvice.recommendedTp)); setPlanResult(null); }}>Apply suggested TP ({liveModelAdvice.recommendedTp})</button>}><span className={labelClass}>Prefill tensor parallel size</span><input className={inputClass} value={prefillTensorParallelVariants} placeholder="4" onChange={(event) => setPrefillTensorParallelVariants(event.target.value)} /><span className="mt-2 flex items-center gap-2"><span className="text-[10px] text-slate-500">Quick select</span>{['1', '1,2', '1,2,4'].map(value => <button key={value} type="button" disabled={false} onClick={() => setPrefillTensorParallelVariants(value)} aria-pressed={prefillTensorParallelVariants === value} className={`rounded-md border px-2.5 py-1.5 text-[10px] transition disabled:opacity-40 ${prefillTensorParallelVariants === value ? 'border-cyan-400/60 bg-cyan-400/10 text-cyan-200' : 'border-slate-700 text-slate-400 hover:border-cyan-500/50 hover:text-cyan-200'}`}>{value}</button>)}</span></ConfigurationField></>}
<ConfigurationField field="replicaVariants"><span className={labelClass}>{selection.guide === 'pd-disaggregation' ? 'Decode replicas' : 'Replicas'}</span><input className={inputClass} value={replicaVariants} disabled={selection.guide === 'pd-disaggregation' && Boolean(pdTopologyVariants.trim())} placeholder="1,2,3" onChange={(event) => setReplicaVariants(event.target.value)} /><span className="mt-2 flex items-center gap-2"><span className="text-[10px] text-slate-500">Quick select</span>{['1', '1,2', '1,2,3'].map(value => <button key={value} type="button" disabled={selection.guide === 'pd-disaggregation' && Boolean(pdTopologyVariants.trim())} onClick={() => setReplicaVariants(value)} aria-pressed={replicaVariants === value} className={`rounded-md border px-2.5 py-1.5 text-[10px] transition disabled:opacity-40 ${replicaVariants === value ? 'border-cyan-400/60 bg-cyan-400/10 text-cyan-200' : 'border-slate-700 text-slate-400 hover:border-cyan-500/50 hover:text-cyan-200'}`}>{value}</button>)}</span></ConfigurationField><ConfigurationField field="tensorParallelVariants" suggestion={liveModelAdvice && String(tensorParallelVariants).split(',').some(value => Number(value) < liveModelAdvice.recommendedTp) && <button type="button" className="mt-1 block text-xs text-amber-300 underline" onClick={() => { setTensorParallelVariants(String(liveModelAdvice.recommendedTp)); setPlanResult(null); }}>Apply suggested TP ({liveModelAdvice.recommendedTp})</button>}><span className={labelClass}>{selection.guide === 'pd-disaggregation' ? 'Decode tensor parallel size' : 'Tensor parallel size'}</span><input className={inputClass} value={tensorParallelVariants} placeholder="4" onChange={(event) => setTensorParallelVariants(event.target.value)} /><span className="mt-2 flex items-center gap-2"><span className="text-[10px] text-slate-500">Quick select</span>{['1', '1,2', '1,2,4'].map(value => <button key={value} type="button" disabled={false} onClick={() => setTensorParallelVariants(value)} aria-pressed={tensorParallelVariants === value} className={`rounded-md border px-2.5 py-1.5 text-[10px] transition disabled:opacity-40 ${tensorParallelVariants === value ? 'border-cyan-400/60 bg-cyan-400/10 text-cyan-200' : 'border-slate-700 text-slate-400 hover:border-cyan-500/50 hover:text-cyan-200'}`}>{value}</button>)}</span></ConfigurationField>
                            </div>
                        </fieldset>}
                        {!singleConfiguration && <TopologyPreview variants={topologyPreviewVariants} isPd={selection.guide === 'pd-disaggregation'} />}
                        <details className="configuration-disclosure group/advanced mt-4 overflow-hidden rounded-xl border border-cyan-500/40">
                            <summary className="cursor-pointer list-none px-5 py-4 text-sm font-semibold text-cyan-100 [&::-webkit-details-marker]:hidden"><ChevronRight aria-hidden="true" className="float-right mt-1 h-4 w-4 text-cyan-300 transition-transform group-open/advanced:rotate-90" />Advanced settings <span className="ml-2 text-xs text-slate-500">{customRows.length + Object.values({ maxModelLen, maxNumSeqs, gpuMemoryUtilization, blockSize, maxNumBatchedTokens, ...guideSettings }).filter(value => value != null && String(value).trim()).length} overrides</span><span className="mt-1 block text-xs font-normal leading-5 text-slate-400">Optional runtime tuning, environment variables, cache, and routing. Leave blank to use Guide defaults.</span></summary>
                            <div className="space-y-6 border-t border-slate-700/70 p-5">
                            {selection.modelServer === 'vllm' && <>
                                <section><h3 className="text-xs font-semibold uppercase tracking-wide text-violet-300">Runtime batching</h3><p className="mt-2 text-xs text-slate-400">Batching controls for the generated model-server YAML. Leave blank to use Guide defaults.</p><div className="mt-4 grid gap-4 md:grid-cols-2"><ConfigurationField field="blockSize"><span className={labelClass}>Block size (tokens)</span><input type="number" min="1" className={inputClass} value={blockSize} placeholder="Guide default" onChange={(event) => setBlockSize(event.target.value)} /><FieldHelp>vLLM --block-size. Use a value supported by the selected runtime image and accelerator.</FieldHelp></ConfigurationField>
<ConfigurationField field="maxNumBatchedTokens"><span className={labelClass}>Max batched tokens</span><input type="number" min="1" className={inputClass} value={maxNumBatchedTokens} placeholder="Guide default" onChange={(event) => setMaxNumBatchedTokens(event.target.value)} /><FieldHelp>vLLM --max-num-batched-tokens. Leave blank to keep the Guide batching configuration.</FieldHelp></ConfigurationField></div></section>
                                <section className="border-t border-slate-700/60 pt-5"><h3 className="text-xs font-semibold uppercase tracking-wide text-emerald-300">vLLM tuning</h3><p className="mt-2 text-xs text-slate-400">Memory capacity and concurrency. Values apply to both P/D roles.</p><div className="mt-4 grid gap-4 md:grid-cols-3"><ConfigurationField field="maxModelLen"><span className={labelClass}>Max context (tokens)</span><input type="number" min="1" className={inputClass} value={maxModelLen} placeholder="Guide default" onChange={(event) => setMaxModelLen(event.target.value)} /><FieldHelp>Maximum request context. Keep benchmark input plus output within this limit.</FieldHelp></ConfigurationField>
<ConfigurationField field="maxNumSeqs"><span className={labelClass}>Max sequences</span><input type="number" min="1" className={inputClass} value={maxNumSeqs} placeholder="Guide default" onChange={(event) => setMaxNumSeqs(event.target.value)} /><FieldHelp>Higher values can improve throughput but consume more memory.</FieldHelp></ConfigurationField>
<ConfigurationField field="gpuMemoryUtilization"><span className={labelClass}>GPU/XPU memory utilization</span><input type="number" min="0.1" max="1" step="0.01" className={inputClass} value={gpuMemoryUtilization} placeholder="Guide default" onChange={(event) => setGpuMemoryUtilization(event.target.value)} /><FieldHelp>Fraction of device memory vLLM may use.</FieldHelp></ConfigurationField>
</div></section>
                            </>}
                            <section className="border-t border-slate-800 pt-4"><h3 className="text-sm font-medium text-slate-200">Custom runtime overrides <span className="text-xs text-slate-500">({customRows.length})</span></h3>
                            <div className="mt-2">

                                <p className="text-xs leading-5 text-slate-400">Add environment variables or command-line arguments for the model server.</p>
                                <FieldHelp>Overrides affect model servers. Set model and TP above. For per-role values, clear the shared control first. Argument names omit --; leave Value blank for a flag. Environment values may also be empty. Hugging Face variables (HF_*, TRANSFORMERS_*, HUGGING*_*) are managed by the platform administrator and cannot be overridden here.</FieldHelp>
                                <div className="mt-3 space-y-3">
                                    {customRows.map((row, index) => (
                                        <div key={index} className="configuration-override flex items-start gap-2 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                                            <div className="grid min-w-0 flex-1 gap-2 sm:grid-cols-2 lg:grid-cols-[minmax(0,0.8fr)_minmax(0,0.9fr)_minmax(0,1.2fr)_minmax(0,1.5fr)]">
                                                <ConfigurationField field={`custom.${index}.target`} className="min-w-0"><span className="text-[10px] text-slate-400">Target</span><select className={inputClass} value={row.target} onChange={(event) => updateCustomRows(customRows.map((item, i) => i === index ? { ...item, target: event.target.value } : item))}>{selection.guide === 'pd-disaggregation' ? <><option value="both">Both roles</option><option value="prefill">Prefill</option><option value="decode">Decode</option></> : <><option value="both">Model server</option>{row.target !== 'both' && <option value={row.target}>{row.target === 'decode' ? 'Model server' : 'Prefill (unsupported)'}</option>}</>}</select></ConfigurationField>
                                                <ConfigurationField field={`custom.${index}.kind`} className="min-w-0"><span className="text-[10px] text-slate-400">Type</span><select className={inputClass} value={row.kind} onChange={(event) => updateCustomRows(customRows.map((item, i) => i === index ? { ...item, kind: event.target.value } : item))}><option value="environment">Environment</option><option value="argument">Argument</option></select></ConfigurationField>
                                                <ConfigurationField field={`custom.${index}.name`} className="min-w-0"><span className="text-[10px] text-slate-400">Name</span><input className={inputClass} placeholder={row.kind === 'argument' ? 'max-model-len' : 'VLLM_LOGGING_LEVEL'} value={row.name} onChange={(event) => updateCustomRows(customRows.map((item, i) => i === index ? { ...item, name: event.target.value } : item))} /></ConfigurationField>
                                                <ConfigurationField field={`custom.${index}.value`} className="min-w-0"><span className="text-[10px] text-slate-400">Value</span><input className={inputClass} placeholder={row.kind === 'argument' ? 'Value or empty for flag' : 'Environment value'} value={row.value} onChange={(event) => updateCustomRows(customRows.map((item, i) => i === index ? { ...item, value: event.target.value } : item))} /></ConfigurationField>
                                            </div>
                                            <button type="button" title="Remove override" aria-label={`Remove override ${index + 1}${row.name ? `: ${row.name}` : ''}`} className="mb-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-slate-500 transition hover:bg-rose-500/10 hover:text-rose-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-400" onClick={() => updateCustomRows(customRows.filter((_, i) => i !== index))}><Trash2 className="h-4 w-4" aria-hidden="true" /></button>
                                        </div>
                                    ))}
                                </div>
                                <button type="button" className="mt-3 rounded-md border border-cyan-500/40 bg-cyan-500/5 px-3 py-1.5 text-[10px] font-semibold text-cyan-200 transition hover:bg-cyan-500/10" onClick={() => { setValidationSubmitted(false); updateCustomRows([...customRows, { target: 'both', kind: 'environment', name: '', value: '' }]); }}>+ Add override</button>
                            </div>

                            </section>
                            {((selection.guide === 'tiered-prefix-cache' && ['native/cpu/base', 'lmcache-connector/cpu/base'].includes(guideVariant)) || (selection.guide === 'pd-disaggregation' && guideVariant === 'vllm-rdma') || guideSettings.cacheCpuGiB || guideSettings.rdmaNicCount) && <section className="border-t border-slate-800 pt-4"><h3 className="text-sm font-medium text-slate-200">Cache &amp; network</h3><div className="mt-3 grid gap-3 md:grid-cols-2">
                                {((selection.guide === 'tiered-prefix-cache' && ['native/cpu/base', 'lmcache-connector/cpu/base'].includes(guideVariant)) || guideSettings.cacheCpuGiB) && <ConfigurationField field="cacheCpuGiB" className="block"><span className={labelClass}>CPU cache capacity per pod (GiB)</span><input type="number" min="0.1" step="0.1" className={inputClass} value={guideSettings.cacheCpuGiB ?? ''} placeholder="Guide default" onChange={event => setGuideSettings(current => ({ ...current, cacheCpuGiB: event.target.value }))} /></ConfigurationField>}
                                {((selection.guide === 'pd-disaggregation' && guideVariant === 'vllm-rdma') || guideSettings.rdmaNicCount) && <ConfigurationField field="rdmaNicCount" className="block"><span className={labelClass}>RDMA NICs per pod</span><input type="number" min="1" step="1" className={inputClass} value={guideSettings.rdmaNicCount ?? ''} placeholder="Guide default" onChange={event => setGuideSettings(current => ({ ...current, rdmaNicCount: event.target.value }))} /><FieldHelp>Independent of TP. The Guide's NIC selectors and alignment constraints are preserved.</FieldHelp></ConfigurationField>}
                            </div></section>}
                            <section className="border-t border-slate-800 pt-4"><h3 className="text-sm font-medium text-slate-200">Router configuration <span className="text-xs text-slate-500">{guideSettings.routerValues?.trim() ? 'Customized' : 'Guide default'}</span></h3><div className="mt-3"><ConfigurationField field="routerValues" className="block"><span className={labelClass}>Router values (YAML overrides)</span><textarea aria-label="Router values" rows={4} className="mt-1 w-full rounded border border-slate-700 bg-slate-950 p-2 font-mono text-xs text-slate-200" value={guideSettings.routerValues ?? ''} placeholder={'router:\n  epp:\n    replicas: 1'} onChange={event => setGuideSettings(current => ({ ...current, routerValues: event.target.value }))} /><FieldHelp>Merged over this Guide's router values and saved with the configuration. Precise routing keeps tokenizer identity and index block size aligned with the model server.</FieldHelp></ConfigurationField></div></section>
                            </div>
                        </details>
                        {embedded && modelSource === 'auto-cache' && modelCacheValidation.state !== 'ready' && <p className="mt-3 text-xs text-amber-300">{modelCacheValidation.state === 'loading' ? 'Checking model cache…' : modelCacheValidation.message || 'Select a ready model cache in Evaluation Setup.'}</p>}
                        {embedded && !catalogLoading && !selectedModelServer && <p className="mt-3 text-xs text-amber-300">This Guide does not support the runtime selected in Setup. Choose another Guide or edit Setup.</p>}
                        {!embedded && validationSubmitted && fieldErrors.length > 0 && <div role="alert" className="mt-3 flex items-center justify-between gap-3 text-xs text-rose-300"><span>{new Set(fieldErrors.map(error => error.field)).size} fields need attention</span><button type="button" className="underline" onClick={() => focusConfigurationError(editorRef.current, fieldErrors, true)}>Next error</button></div>}
                        {!embedded && <div className="mt-4 flex justify-end">
                            <ResourceBasisControl value={resourceBasis} hardware={clusterHardware} requiredCards={Math.max(1, ...topologyPreviewVariants.map(row => row.gpuCount))} disabled={operationBusy || catalogLoading} loading={planning} onGenerate={generate} label="Generate YAML" />
                        </div>}
                        </fieldset>
                    </section>

                    {planError && (
                        <div className="flex items-center gap-2 border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">
                            <AlertTriangle className="h-4 w-4" /> {planError}
                        </div>
                    )}

                    {!embedded && planResult && (
                        <section className="border border-slate-800 bg-slate-950/40 p-4">
                            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                                <h2 className="text-sm font-semibold">Generated deployable configuration</h2>
                                <span className={`border px-2 py-0.5 text-[10px] ${validation.status === 'invalid' ? 'border-red-500/40 text-red-300' : validation.status === 'warnings' ? 'border-amber-500/40 text-amber-300' : 'border-emerald-500/40 text-emerald-300'}`}>
                                    {planResult.status || validation.status || 'planned'}
                                </span>
                            </div>
                            <div className="mb-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-3 lg:grid-cols-6">
                                <div><span className="block text-slate-600">Selected accelerator</span><span className="text-slate-300">{String(planResult.source?.accelerator || selection.accelerator).toUpperCase()} · {summary.availableAccelerators ?? planResult.machineProfile?.accelerator?.count ?? 0} available{planResult.machineProfile?.accelerator?.model ? ` · ${planResult.machineProfile.accelerator.model}` : ''}</span></div>
                                <div><span className="block text-slate-600">Replicas</span><span className="text-slate-300">{selection.guide === 'pd-disaggregation' ? `P ${summary.plannedReplicasByRole?.prefill ?? prefillReplicas} · D ${summary.plannedReplicasByRole?.decode ?? replicas}` : `${summary.guideReplicas ?? '—'} → ${summary.plannedReplicas ?? '—'}${summary.maxRecommendedReplicas != null ? ` · max ${summary.maxRecommendedReplicas}` : ''}`}</span></div>
                                <div><span className="block text-slate-600">Tensor parallel</span><span className="text-slate-300">{selection.guide === 'pd-disaggregation' ? `P TP ${summary.tensorParallelSizeByRole?.prefill ?? prefillTensorParallelSize} · D TP ${summary.tensorParallelSizeByRole?.decode ?? tensorParallelSize}` : `TP ${summary.tensorParallelSize ?? tensorParallelSize}`}</span></div>
                                <div><span className="block text-slate-600">Recommended TP</span><span className={summary.recommendedTensorParallelSize > Number(tensorParallelSize) ? 'text-red-300' : 'text-emerald-300'}>{summary.recommendedTensorParallelSize ? `TP ≥ ${summary.recommendedTensorParallelSize}` : 'Unknown'}</span></div>
                                <div><span className="block text-slate-600">Estimated model footprint</span><span className="text-slate-300">{summary.modelEstimate?.estimatedWeightMemoryGiB ? `${summary.modelEstimate.estimatedWeightMemoryGiB} GiB` : 'Unknown'}</span></div>
                                <div><span className="block text-slate-600">Requested accelerators</span><span className={summary.requestedAccelerators > summary.availableAccelerators ? 'text-red-300' : 'text-slate-300'}>{summary.requestedAccelerators ?? '—'} / {summary.availableAccelerators ?? '—'}</span></div>
                            </div>
                            {(validation.errors || []).filter(error => !serverFieldErrors.some(item => item.message === error) && !String(planError || '').includes(error)).map((error) => <p key={error} className="mb-1 text-xs text-red-300">Error: {error}</p>)}
                            {(validation.warnings || []).map((warning) => <p key={warning} className="mb-1 text-xs text-amber-300">Warning: {warning}</p>)}
                            {summary.recommendedTensorParallelSize && (summary.recommendedTensorParallelSize > Number(tensorParallelSize) || (selection.guide === 'pd-disaggregation' && summary.recommendedTensorParallelSize > Number(prefillTensorParallelSize))) && <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border border-cyan-500/30 bg-cyan-500/5 p-3"><p className="text-xs text-cyan-100">Recommended topology for this model and selected card budget: {selection.guide === 'pd-disaggregation' ? `P TP ${summary.recommendedTensorParallelSize} · D TP ${summary.recommendedTensorParallelSize}` : `${Math.max(1, Math.min(Number(replicas), summary.maxRecommendedReplicas || 1))} replica × TP ${summary.recommendedTensorParallelSize}`}.</p><button type="button" onClick={() => { const nextReplicas = Math.max(1, Math.min(Number(replicas), summary.maxRecommendedReplicas || 1)); setReplicas(nextReplicas); setReplicaVariants(String(nextReplicas)); setTensorParallelSize(summary.recommendedTensorParallelSize); setTensorParallelVariants(String(summary.recommendedTensorParallelSize)); if (selection.guide === 'pd-disaggregation') { setPrefillTensorParallelSize(summary.recommendedTensorParallelSize); setPrefillTensorParallelVariants(String(summary.recommendedTensorParallelSize)); } setPlanResult(null); }} className="border border-cyan-500 px-3 py-1.5 text-[10px] font-semibold text-cyan-200">Apply recommendation</button></div>}
                            <div className="mt-3">
                                <div className="mb-1 flex items-center gap-2 text-[11px] text-slate-400">
                                    <FileCode className="h-3.5 w-3.5" /> Deployment configuration preview (published artifact is the deployment source)
                                </div>
                                <YamlPreview content={planResult.plannedDeployment?.content || ''} prominent />
                            </div>
                            <div className="mt-4 flex flex-wrap items-center justify-end gap-3">
                                <button type="button" disabled={validation.status === 'invalid' || publishing} onClick={() => sendGuideToEvaluate()}
                                    className="inline-flex h-9 items-center gap-2 border border-emerald-500 bg-emerald-500/10 px-3 text-xs text-emerald-200 disabled:opacity-40">
                                    {publishing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />} {embedded ? 'Save configuration' : 'Publish and return to Evaluate'}
                                </button>
                            </div>
                        </section>
                    )}
                </div>

            {embedded && onPublished && !singleConfiguration && <div id="optimizationSelection"><OptimizationComparisonTree guide={selection.guide} selected={optimizationSelection} onChange={setOptimizationSelection} disabled={operationBusy} /></div>}

            {embedded && <footer className="flex flex-wrap items-center justify-between gap-3 py-2">
                <div className="min-w-0 flex-1" role="status" aria-live="polite">
                    {!operationBusy && validationSubmitted && fieldErrors.length > 0 && <button type="button" className="text-xs text-rose-300 underline" onClick={() => focusConfigurationError(editorRef.current, fieldErrors, true)}>{new Set(fieldErrors.map(error => error.field)).size} fields need attention · Next error</button>}
                    {operationBusy && <><p className="flex items-center gap-2 text-xs text-cyan-200"><Loader2 className="h-3.5 w-3.5 animate-spin" />{progress || 'Generating deployable YAML…'} <span className="text-slate-500">{elapsedSeconds}s</span></p>{elapsedSeconds >= 15 && <p className="mt-1 text-[10px] text-slate-400">Still working. The Guide source and cluster checks may take a moment.</p>}</>}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                    <ResourceBasisControl value={resourceBasis} hardware={clusterHardware} requiredCards={Math.max(1, ...topologyPreviewVariants.map(row => row.gpuCount))} disabled={operationBusy || catalogLoading} loading={planning} onGenerate={generate} />


                </div>
            </footer>}
            {embedded && planResult && <section className="min-w-0">
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800/70 pb-3">
                    <h3 className="text-sm font-medium text-slate-200">YAML preview · {optimizationSelection.includes('full') || singleConfiguration ? 'Full Guide scenario' : 'Source template'}</h3>
                    <span className="text-sm text-cyan-200">{planResult.summary?.plannedReplicasByRole?.prefill ? `Prefill ${planResult.summary.plannedReplicasByRole.prefill}R × TP${planResult.summary.tensorParallelSizeByRole?.prefill} · Decode ${planResult.summary.plannedReplicasByRole.decode}R × TP${planResult.summary.tensorParallelSizeByRole?.decode}` : `${planResult.summary?.plannedReplicas ?? '—'} replicas × TP${planResult.summary?.tensorParallelSize ?? '—'}`}</span>
                </div>
                {onPublished && !singleConfiguration && <p className="mt-3 text-xs text-slate-400">Source template{topologyPreviewVariants.length > 1 ? ' · one topology from this sweep' : ''}. Your {optimizationSelection.length} selected scenario{optimizationSelection.length === 1 ? '' : 's'} use separate deployment variants at execution.</p>}
                {(validation.errors || []).filter(error => !serverFieldErrors.some(item => item.message === error) && !String(planError || '').includes(error)).map((error) => <p key={error} className="mt-2 text-xs text-rose-300">{error}</p>)}
                {(validation.warnings || []).map((warning) => <p key={warning} className="mt-2 text-xs text-amber-300">{warning}</p>)}
                {planResult.deploymentBundle && <div className="mt-3">
                    <div className="flex items-center gap-2 text-xs text-slate-400"><span>Router and auxiliary deployment files</span><span className="ml-auto text-[10px]">{(planResult.deploymentBundle.helm?.values?.length || 0) + (planResult.deploymentBundle.resources?.length || 0)} files</span></div>
                    <p className="mt-1 text-[10px] text-slate-500">Source {planResult.deploymentBundle.sourceCommit} · Router {planResult.deploymentBundle.helm?.version}. These inputs are saved and reused during deployment.</p>
                    {[...(planResult.deploymentBundle.helm?.values || []), ...(planResult.deploymentBundle.resources || [])].map(file => <YamlPreview key={file.name} title={file.name} content={file.content} defaultOpen={false} />)}
                </div>}
                <YamlPreview content={planResult.plannedDeployment?.content || ''} prominent />
            </section>}
            {embedded && planResult?.plannedDeployment?.content && <div className="flex justify-end gap-2">
                <button type="button" disabled={validation.status === 'invalid' || publishing} onClick={() => sendGuideToEvaluate('another')} className="inline-flex h-9 items-center rounded-lg border border-slate-700 px-4 text-xs text-slate-200 hover:border-cyan-500/50 disabled:opacity-40">Save &amp; add another</button>
                <button type="button" disabled={validation.status === 'invalid' || publishing} onClick={() => sendGuideToEvaluate(true)} className="inline-flex h-9 items-center gap-1 rounded-lg bg-cyan-400 px-4 text-xs font-semibold text-slate-950 hover:bg-cyan-300 disabled:opacity-40">
                    {publishing ? <Loader2 className="h-4 w-4 animate-spin" /> : null}Next<ChevronRight className="h-3.5 w-3.5" />
                </button>
            </div>}

            <div className="flex justify-end border border-slate-800 bg-slate-900/40 px-4 py-3 text-xs text-slate-500">
                <button onClick={() => onNavigate('optimization-evaluate')} className="inline-flex items-center gap-1 text-slate-400 hover:text-slate-200">
                    Open Evaluate <ChevronRight className="h-3 w-3" />
                </button>
            </div>
            {embedded && planResult && <div ref={generatedYamlEndRef} />}
        </section>
        </ConfigurationValidationContext.Provider>
    );
}
