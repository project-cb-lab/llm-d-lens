import { requestJson } from '../api/httpClient';
import { openClusterSession } from './OptimizationWorkspace/clusterBackend';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { usePolling } from '../hooks/usePolling';
import { ArrowLeft, Box, CheckCircle2, ChevronDown, Cpu, Database, Rocket, Sparkles } from 'lucide-react';
import { Button, Input, ModuleHeader, ModulePage, Badge } from './ui';
import { CATEGORIES, MODELS } from '../data/modelCatalog';
import { listStorageVolumes } from './StorageManagement/storageManagementBackend';
import { listModelCacheEntries } from './ModelCache/modelCacheBackend';
import { StorageVolumeSelect } from './common/StorageVolumeSelect';
import AgenticDeploymentWorkspace from './AgenticDeploymentWorkspace';
import { storeEvaluationIntent } from '../features/evaluation/transfer';
import { consumePendingModelMarketCluster } from '../features/modelMarket/transfer';
import { useAuth } from '../features/auth/useAuth';
import ResourceBasisControl from './evaluation/ResourceBasisControl.jsx';
import { recommendationBudget } from '../features/evaluation/experimentDesign.js';

const formatDefaultName = (model) => `${model.id}-${new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '')}`;
const GiB = 1024 ** 3;
const VLLM_ARGUMENT_NAME = /^[a-z][a-z0-9-]*$/;

function parseVllmArguments(input) {
    const argumentsByName = new Map();
    const lines = input.split('\n').map((line) => line.trim()).filter(Boolean);
    for (const line of lines) {
        const match = line.match(/^--?([a-z][a-z0-9-]*)(?:[=\s]+(.+))?$/);
        if (!match || !VLLM_ARGUMENT_NAME.test(match[1])) {
            return { arguments: [], error: `Invalid vLLM parameter: ${line}` };
        }
        argumentsByName.set(match[1], match[2]?.trim() || 'true');
    }
    return { arguments: [...argumentsByName].map(([name, value]) => ({ name, value })), error: '' };
}

function standardRecommendation(model, hardware, resourceBasis = 'available') {
    const availableGpus = recommendationBudget(hardware, resourceBasis);
    const totalVramGiB = (hardware?.vramBytes || 0) / GiB;
    const vramPerGpuGiB = availableGpus > 0 ? totalVramGiB / availableGpus : 0;
    const requiredVramGiB = Number.isFinite(model.sizeGiB) && model.sizeGiB > 0 ? model.sizeGiB * 1.2 : 0;
    const tensorParallelism = vramPerGpuGiB > 0 ? Math.max(1, Math.ceil(requiredVramGiB / vramPerGpuGiB)) : 1;
    const replicas = availableGpus >= tensorParallelism
        ? Math.min(4, Math.floor(availableGpus / tensorParallelism))
        : 1;
    return {
        tensorParallelism,
        replicas,
        maxModelLen: model.context === '128K' ? 8192 : 4096,
        cpuOffload: requiredVramGiB > totalVramGiB && totalVramGiB > 0,
        availableGpus,
        vramPerGpuGiB,
    };
}

export function ModelCard({ model, onSelect }) {
    return (
        <button
            type="button"
            onClick={() => onSelect(model)}
            className="group flex min-h-52 flex-col rounded-lg border border-slate-800 bg-slate-900/70 p-5 text-left transition hover:border-cyan-400/60 hover:bg-slate-900 focus:outline-none focus:ring-2 focus:ring-cyan-400"
        >
            <div className="flex items-start justify-between gap-4">
                <div>
                    <p className="text-xs font-medium uppercase text-cyan-300">{model.family}</p>
                    <h2 className="mt-2 text-lg font-semibold text-white">{model.name}</h2>
                </div>
                <Box size={19} className="text-slate-500 transition group-hover:text-cyan-300" aria-hidden="true" />
            </div>
            <dl className="mt-5 grid grid-cols-2 gap-x-3 gap-y-4 text-xs">
                <div><dt className="text-slate-500">Released</dt><dd className="mt-1 text-slate-200">{model.releaseDate}</dd></div>
                <div><dt className="text-slate-500">Precision</dt><dd className="mt-1 text-slate-200">{model.dtype}</dd></div>
                <div><dt className="text-slate-500">Weights</dt><dd className="mt-1 text-slate-200">{model.weightGiB ? `${model.weightGiB} GiB` : 'Not listed'}</dd></div>
                <div><dt className="text-slate-500">Context</dt><dd className="mt-1 text-slate-200">{model.contextLength ? model.contextLength.toLocaleString() : 'Not listed'}</dd></div>
                <div className="col-span-2"><dt className="text-slate-500">Model type</dt><dd className="mt-1 text-slate-200">{model.modelType}</dd></div>
            </dl>
        </button>
    );
}

export default function ModelMarketPage({ onNavigate }) {
    const { can } = useAuth();
    const canDeploy = can('deployment:run:create');
    const [selected, setSelected] = useState(() => {
        try {
            const draft = JSON.parse(sessionStorage.getItem('prism_model_market_return_draft') || 'null');
            sessionStorage.removeItem('prism_model_market_return_draft');
            return MODELS.find((model) => model.repository === draft?.model) || null;
        } catch {
            sessionStorage.removeItem('prism_model_market_return_draft');
            return null;
        }
    });
    const [category, setCategory] = useState('All');
    const [deployName, setDeployName] = useState('');
    const [deploymentDescription, setDeploymentDescription] = useState('');
    const [cluster, setCluster] = useState('');
    const [replicas, setReplicas] = useState(1);
    const [tensorParallelism, setTensorParallelism] = useState(1);
    const [mode, setMode] = useState('standard');
    const [resourceBasis, setResourceBasis] = useState('available');
    const [modelCacheVolumes, setModelCacheVolumes] = useState([]);
    const [storageVolumeId, setStorageVolumeId] = useState('');
    const [storageLoading, setStorageLoading] = useState(false);
    const [cacheCheckLoading, setCacheCheckLoading] = useState(false);
    const [cacheReady, setCacheReady] = useState(false);
    const [cacheCheckError, setCacheCheckError] = useState('');
    const [vllm, setVllm] = useState({
        gpuMemoryUtilization: 0.9,
        maxModelLen: null,
        maxNumSeqs: 256,
        maxNumBatchedTokens: 8192,
        enablePrefixCaching: true,
        enforceEager: false,
        enableAutoToolChoice: false,
        toolCallParser: 'hermes',
    });
    const [vllmParametersOpen, setVllmParametersOpen] = useState(false);
    const [customVllmArguments, setCustomVllmArguments] = useState('');
    const [environmentOpen, setEnvironmentOpen] = useState(false);
    const [useClusterProxy, setUseClusterProxy] = useState(true);
    const [proxy, setProxy] = useState({ httpProxy: '', httpsProxy: '', noProxy: '' });
    const [clusters, setClusters] = useState([]);
    const [hardware, setHardware] = useState(null);
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState('');
    const [agenticDraft, setAgenticDraft] = useState(null);
    const [pendingCluster] = useState(() => consumePendingModelMarketCluster());

    useEffect(() => {
        if (!selected) return;
        const draft = JSON.parse(sessionStorage.getItem('prism_model_market_return_draft_values') || 'null');
        sessionStorage.removeItem('prism_model_market_return_draft_values');
        if (!draft) return;
        setDeployName(draft.deployName || formatDefaultName(selected));
        setDeploymentDescription(draft.description || '');
        setCluster(draft.cluster || '');
        setReplicas(Number(draft.replicas) || 1);
        setTensorParallelism(Number(draft.tp) || 1);
        setStorageVolumeId(draft.storageVolumeId || '');
        setMode(draft.mode === 'advanced' ? 'advanced' : 'standard');
    }, [selected]);

    const loadClusters = useCallback(async () => {
        try {
            const payload = await requestJson('/api/cluster/clusters');
            const items = payload.items || [];
            setClusters(items);
            const preferredClusterId = pendingCluster && items.some((item) => item.id === pendingCluster)
                ? pendingCluster
                : items.find((item) => item.ready)?.id;
            setCluster((current) => current || preferredClusterId || '');
        } catch (failure) {
            setError(failure.message);
        }
    }, [pendingCluster]);

    useEffect(() => {
        loadClusters();
    }, [loadClusters]);
    usePolling(loadClusters);

    const plan = useMemo(() => selected && standardRecommendation(selected, hardware, resourceBasis), [selected, hardware, resourceBasis]);
    const visibleModels = useMemo(() => MODELS.filter((model) => category === 'All' || model.category === category), [category]);
    const availableGpuCount = recommendationBudget(hardware, resourceBasis);
    const hasGpuCapacity = Number.isInteger(availableGpuCount);
    const requiresModelCache = mode === 'standard' || mode === 'agentic';
    const requestedGpuCount = replicas * tensorParallelism;
    const parsedVllmArguments = parseVllmArguments(customVllmArguments);
    const vllmArguments = [...new Map([
        ...(selected?.vllmArguments || []).map((argument) => [argument.name, argument]),
        ...(vllm.enableAutoToolChoice ? [
            { name: 'enable-auto-tool-choice', value: 'true' },
            { name: 'tool-call-parser', value: vllm.toolCallParser },
        ] : []).map((argument) => [argument.name, argument]),
        ...parsedVllmArguments.arguments.map((argument) => [argument.name, argument]),
    ]).values()];
    const vllmArgumentControls = <>
        <fieldset className="border-t border-slate-800 pt-3">
            <legend className="text-xs font-medium text-slate-300">Tool calling</legend>
            <label className="mt-3 flex items-center justify-between gap-3 text-xs text-slate-300"><span>Enable automatic tool choice</span><input className="h-4 w-4 accent-cyan-400" type="checkbox" checked={vllm.enableAutoToolChoice} onChange={(event) => setVllm((current) => ({ ...current, enableAutoToolChoice: event.target.checked }))} /></label>
            {vllm.enableAutoToolChoice && <label className="mt-3 block text-xs text-slate-400">Tool call parser<input className="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 text-sm text-slate-200" value={vllm.toolCallParser} onChange={(event) => setVllm((current) => ({ ...current, toolCallParser: event.target.value }))} required /></label>}
        </fieldset>
        <label className="border-t border-slate-800 pt-3 text-xs text-slate-400">Custom vLLM parameters<textarea className="mt-1 min-h-24 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 font-mono text-xs text-slate-200" value={customVllmArguments} onChange={(event) => setCustomVllmArguments(event.target.value)} placeholder={'--max-log-len 4096\n--enable-chunked-prefill'} /></label>
        {parsedVllmArguments.error && <p role="alert" className="text-xs text-rose-300">{parsedVllmArguments.error}</p>}
    </>;
    const topologyError = hasGpuCapacity && plan && (tensorParallelism > availableGpuCount || requestedGpuCount > availableGpuCount)
        ? `This topology needs ${requestedGpuCount} GPUs (${replicas} replica${replicas === 1 ? '' : 's'} x TP ${tensorParallelism}), but the cluster has ${availableGpuCount} available.`
        : '';

    useEffect(() => {
        if (!cluster) {
            setHardware(null);
            return undefined;
        }
        let cancelled = false;
        requestJson(`/api/cluster/overview?clusterId=${encodeURIComponent(cluster)}`)
            .then((payload) => {
                if (!cancelled) {
                    setHardware(payload.kubernetes?.hardware || null);
                }
            })
            .catch(() => !cancelled && setHardware(null));
        return () => { cancelled = true; };
    }, [cluster]);

    useEffect(() => {
        if (!cluster) {
            setModelCacheVolumes([]);
            setStorageVolumeId('');
            return undefined;
        }
        let cancelled = false;
        setStorageLoading(true);
        setStorageVolumeId('');
        // Fetch every status (not just "ready") so the picker can show
        // non-ready volumes as visible-but-unselectable instead of hiding
        // them entirely.
        listStorageVolumes({ clusterId: cluster, purpose: 'model-cache' })
            .then((volumes) => {
                if (cancelled) return;
                setModelCacheVolumes(volumes);
                // Auto-select the first ready volume so the operator doesn't
                // have to manually pick storage every time a cluster is chosen.
                const firstReady = volumes.find((volume) => volume.status === 'ready');
                if (firstReady) setStorageVolumeId(firstReady.id);
            })
            .catch((failure) => {
                if (!cancelled) {
                    setModelCacheVolumes([]);
                    setError(failure.message || 'Unable to load model cache storage');
                }
            })
            .finally(() => {
                if (!cancelled) setStorageLoading(false);
            });
        return () => { cancelled = true; };
    }, [cluster]);

    useEffect(() => {
        setCacheReady(false);
        setCacheCheckError('');
        if (!selected?.repository || !cluster || !storageVolumeId) {
            setCacheCheckLoading(false);
            return undefined;
        }
        let cancelled = false;
        setCacheCheckLoading(true);
        listModelCacheEntries({ clusterId: cluster, storageVolumeId })
            .then((entries) => {
                if (cancelled) return;
                const expected = selected.repository.trim().toLowerCase();
                setCacheReady(entries.some((entry) => (
                    entry.status === 'ready'
                    && entry.source?.kind === 'huggingface'
                    && String(entry.source?.huggingface?.repoId || '').trim().toLowerCase() === expected
                )));
            })
            .catch((failure) => {
                if (!cancelled) {
                    setCacheReady(false);
                    setCacheCheckError(failure.message || 'Unable to check model cache');
                }
            })
            .finally(() => {
                if (!cancelled) setCacheCheckLoading(false);
            });
        return () => { cancelled = true; };
    }, [selected?.repository, cluster, storageVolumeId]);

    useEffect(() => {
        if (selected && hardware) {
            setReplicas(plan.replicas);
            setTensorParallelism(plan.tensorParallelism);
        }
    }, [selected, hardware, plan]);

    const chooseModel = (model) => {
        setSelected(model);
        setDeployName(formatDefaultName(model));
        setDeploymentDescription('');
        setReplicas(standardRecommendation(model, hardware, resourceBasis).replicas);
        setTensorParallelism(standardRecommendation(model, hardware, resourceBasis).tensorParallelism);
        const isPoolingModel = ['Encoder', 'Rerank'].includes(model.category);
        setVllm((current) => ({
            ...current,
            maxModelLen: standardRecommendation(model, hardware, resourceBasis).maxModelLen,
            maxNumSeqs: isPoolingModel ? 512 : 256,
            maxNumBatchedTokens: isPoolingModel ? 16384 : 8192,
            enablePrefixCaching: !isPoolingModel,
        }));
        // A previously captured Agentic draft snapshots `model` at the moment
        // the Agentic tab was clicked (see the Mode fieldset below). Picking a
        // different model here must invalidate that stale snapshot -- and
        // fall back to Standard -- so the workspace can't silently redeploy
        // whatever model was selected before.
        setMode('standard');
        setAgenticDraft(null);
    };

    const submit = async (requestedResourceBasis = resourceBasis) => {
        setError('');
        if (parsedVllmArguments.error) {
            setError(parsedVllmArguments.error);
            return;
        }
        const requestedGpuBudget = recommendationBudget(hardware, requestedResourceBasis);
        if (mode === 'standard' && (tensorParallelism > requestedGpuBudget || requestedGpuCount > requestedGpuBudget)) {
            setError(`This topology needs ${requestedGpuCount} GPUs (${replicas} replica${replicas === 1 ? '' : 's'} x TP ${tensorParallelism}), but the selected budget has ${requestedGpuBudget} available. Change the replica count or choose a sufficient budget.`);
            return;
        }
        if (requiresModelCache && !storageVolumeId) {
            setError('Select a model cache storage for Standard or Agentic deployment, or choose Advanced to configure another model source.');
            return;
        }
        if (requiresModelCache && !cacheReady) {
            setError('This model is not ready in the selected model cache storage. Ask an admin to download it before deploying.');
            return;
        }
        if (mode === 'advanced') {
            storeEvaluationIntent({
                operation: 'deploy-only',
                return_target: 'model-market',
                name: deployName,
                deployment_name: deployName,
                deployment_description: deploymentDescription,
                cluster_id: cluster,
                workloads: [{ model: selected.repository, replicas, tensor_parallel_size: tensorParallelism }],
                runtime: {
                    model_server: 'vllm',
                    model_source: storageVolumeId ? 'auto-cache' : 'huggingface',
                    storage_volume_id: storageVolumeId,
                },
            });
            onNavigate('optimization-evaluate-new');
            return;
        }
        if (mode === 'agentic') {
            setAgenticDraft({
                model: selected.repository,
                deploymentName: deployName,
                description: deploymentDescription,
                clusterId: cluster,
                modelWeightGiB: selected.sizeGiB,
                contextLength: vllm.maxModelLen || plan.maxModelLen,
                storageVolumeId,
                maxNumSeqs: vllm.maxNumSeqs,
                maxNumBatchedTokens: vllm.maxNumBatchedTokens,
                gpuMemoryUtilization: vllm.gpuMemoryUtilization,
                vllmArguments,
            });
            return;
        }
        setSubmitting(true);
        try {
            const clusterSessionId = await openClusterSession(cluster);
            await requestJson('/api/v1/deployments/standard-vllm-runs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    model: selected.repository,
                    deployment_name: deployName,
                    description: deploymentDescription,
                    cluster_session_id: clusterSessionId,
                    replicas,
                    tensor_parallel_size: tensorParallelism,
                    storage_type: 'model-cache',
                    storage_volume_id: storageVolumeId,
                    max_model_len: vllm.maxModelLen ?? plan.maxModelLen,
                    gpu_memory_utilization: vllm.gpuMemoryUtilization,
                    max_num_seqs: vllm.maxNumSeqs,
                    max_num_batched_tokens: vllm.maxNumBatchedTokens,
                    enable_prefix_caching: vllm.enablePrefixCaching,
                    enforce_eager: vllm.enforceEager,
                    vllm_arguments: vllmArguments,
                    use_cluster_proxy: useClusterProxy,
                    http_proxy: proxy.httpProxy,
                    https_proxy: proxy.httpsProxy,
                    no_proxy: proxy.noProxy,
                }),
            });
            onNavigate('optimization-deployments');
        } catch (failure) {
            setError(failure.message || 'Unable to create deployment');
        } finally {
            setSubmitting(false);
        }
    };

    if (!selected) {
        return (
            <ModulePage contentClassName="flex flex-col gap-5">
                <ModuleHeader icon={Database} title="Model Market" />
                <div className="flex items-center justify-between gap-4">
                    <div><h1 className="text-xl font-semibold text-white">Select a model</h1><p className="mt-1 text-sm text-slate-400">Popular open models across language, vision, image, speech, retrieval, and diffusion.</p></div>
                    <span className="text-xs text-slate-500">{visibleModels.length} models</span>
                </div>
                <div className="flex flex-wrap gap-2" role="tablist" aria-label="Model categories">{CATEGORIES.map((item) => <button key={item} type="button" role="tab" aria-selected={category === item} onClick={() => setCategory(item)} className={`rounded-md border px-3 py-1.5 text-xs ${category === item ? 'border-cyan-400 bg-cyan-400/10 text-cyan-200' : 'border-slate-700 text-slate-400 hover:border-slate-500'}`}>{item}</button>)}</div>
                <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                    {visibleModels.map((model) => <ModelCard key={model.id} model={model} onSelect={chooseModel} />)}
                </section>
            </ModulePage>
        );
    }

    if (!canDeploy) {
        return (
            <ModulePage contentClassName="flex flex-col gap-5">
                <ModuleHeader icon={Rocket} title="One-click deployment" actions={<Button variant="secondary" onClick={() => setSelected(null)}><ArrowLeft size={15} aria-hidden="true" />Model Market</Button>} />
                <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-6">
                    <p className="text-sm font-medium text-slate-200">Your role cannot create deployments.</p>
                    <p className="mt-2 text-sm leading-6 text-slate-400">You can use deployments that already exist or were shared with you. Open the Deployments page to view them, or ask an administrator to grant you the deployment-create permission.</p>
                    <div className="mt-5"><Button variant="sky" onClick={() => onNavigate?.('optimization-deployments')}>Open Deployments</Button></div>
                </div>
            </ModulePage>
        );
    }

    return (
        <ModulePage contentClassName="flex flex-col gap-5">
            <ModuleHeader icon={Rocket} title="One-click deployment" actions={<Button variant="secondary" onClick={() => setSelected(null)}><ArrowLeft size={15} aria-hidden="true" />Model Market</Button>} />
            <div className="grid grid-cols-1 gap-5">
                <form id="model-market-deployment-form" className="rounded-lg border border-slate-800 bg-slate-900/70 p-6" onSubmit={(event) => { event.preventDefault(); submit(); }}>
                    <div className="flex items-start justify-between gap-4 border-b border-slate-800 pb-5"><div><p className="text-xs uppercase text-cyan-300">{selected.family}</p><h1 className="mt-1 text-xl font-semibold text-white">{selected.name}</h1></div><span className="text-sm text-slate-400">{selected.sizeGiB} GiB</span></div>
                    <div className="mt-6 grid gap-5 md:grid-cols-2">
                        <label className="text-sm text-slate-300">Deployment name<Input className="mt-2" value={deployName} onChange={(event) => setDeployName(event.target.value)} required /></label>
                        <label className="text-sm text-slate-300 md:col-span-2">Description<textarea className="mt-2 min-h-24 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200 outline-none transition placeholder:text-slate-600 focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/10" value={deploymentDescription} onChange={(event) => setDeploymentDescription(event.target.value)} maxLength={1000} placeholder="Optional deployment description" /></label>
                        <label className="text-sm text-slate-300">Cluster<select className="mt-2 h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-slate-200" value={cluster} onChange={(event) => setCluster(event.target.value)} required><option value="">Select cluster</option>{clusters.map((item) => <option key={item.id} value={item.id} disabled={!item.ready}>{item.name}{item.ready ? '' : ' (not ready)'}</option>)}</select></label>
                        {mode !== 'agentic' && <><label className="order-3 text-sm text-slate-300">Replicas<input className="mt-2 h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-slate-200" type="number" min="1" max="32" value={replicas} onChange={(event) => setReplicas(Number(event.target.value))} /></label><label className="order-3 text-sm text-slate-300">Tensor parallelism<input className="mt-2 h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-slate-200" type="number" min="1" max="32" value={tensorParallelism} onChange={(event) => setTensorParallelism(Number(event.target.value))} /></label></>}
                        <label className="order-2 md:col-span-2 text-sm text-slate-300">Model cache storage <span className="text-xs text-slate-500">{requiresModelCache ? 'Required for Standard or Agentic' : 'Optional for Advanced'}</span><p className="mt-1 text-xs leading-5 text-slate-500">Select a registered hostPath or NFS volume containing downloaded model weights. This is not the Tiered Prefix Cache KV filesystem.</p><StorageVolumeSelect clearable className="mt-2" value={storageVolumeId} onChange={setStorageVolumeId} volumes={modelCacheVolumes} loading={storageLoading} disabled={!cluster} placeholder={requiresModelCache ? 'Select registered model storage' : 'No model storage selected'} emptyLabel={storageLoading ? 'Loading model cache storage...' : 'No ready model-cache storage'} optionDetail={(volume) => volume.capacity || volume.kind} /><div className="mt-2 flex gap-3">{storageVolumeId && <button type="button" onClick={() => setStorageVolumeId('')} className="text-xs text-slate-400 underline hover:text-cyan-300">Clear selection</button>}<button type="button" onClick={() => onNavigate('storage-management')} className="text-xs text-cyan-300 underline">Manage storage</button></div>{!storageLoading && cluster && modelCacheVolumes.length === 0 && <p role="alert" className="mt-2 text-xs text-amber-300">No registered model-cache volume exists. Open Manage storage, create a Model Cache volume, and enter an existing host path such as /mnt/data/huggingface-cache or an NFS export.</p>}{storageVolumeId && cacheCheckLoading && <p className="mt-2 text-xs text-slate-500">Checking whether this model is cached...</p>}{storageVolumeId && cacheCheckError && <p role="alert" className="mt-2 text-xs text-rose-300">{cacheCheckError}</p>}{storageVolumeId && !cacheCheckLoading && !cacheCheckError && !cacheReady && <p role="alert" className="mt-2 text-xs text-amber-300">This model is not downloaded in the selected storage. Clear it or download the model there first.</p>}{storageVolumeId && !cacheCheckLoading && cacheReady && <p className="mt-2 text-xs text-emerald-300">This model is ready in the selected cache.</p>}</label>
                    </div>
                    {!selected.vllmSupported && <p role="alert" className="mt-5 text-sm text-amber-300">{selected.type} requires the {selected.runtime} runtime. Raw vLLM Standard deployment is unavailable until this provider is added.</p>}
                    {mode === 'standard' && topologyError && <p role="alert" className="mt-5 text-sm text-amber-300">{topologyError} The recommended TP remains TP {plan.tensorParallelism}; modify replicas or TP before creating this deployment.</p>}
                    <fieldset className="mt-6"><legend className="text-sm text-slate-300">Mode</legend><div className="mt-2 grid gap-2 md:grid-cols-3">{[['standard', 'Standard', 'Raw vLLM baseline', false], ['advanced', 'Advanced', 'Evaluate configuration', false], ['agentic', 'Agentic', 'Recommended guide and topology', true]].map(([value, label, description, experimental]) => <button key={value} type="button" onClick={() => {
                        if (value === 'agentic') {
                            setMode(value);
                            setAgenticDraft({ model: selected.repository, deploymentName: deployName, description: deploymentDescription, clusterId: cluster, modelWeightGiB: selected.sizeGiB, contextLength: vllm.maxModelLen || plan.maxModelLen, storageVolumeId, maxNumSeqs: vllm.maxNumSeqs, maxNumBatchedTokens: vllm.maxNumBatchedTokens, gpuMemoryUtilization: vllm.gpuMemoryUtilization, vllmArguments: selected.vllmArguments || [] });
                            return;
                        }
                        setMode(value);
                    }} className={`rounded-lg border p-3 text-left ${mode === value ? 'border-cyan-400 bg-cyan-400/10' : 'border-slate-700 bg-slate-950 hover:border-slate-600'}`}><span className="flex items-center gap-2 text-sm font-medium text-white">{label}{experimental && <Badge tone="warning" size="xs">Experimental</Badge>}</span><span className="mt-1 block text-xs text-slate-500">{description}</span></button>)}</div></fieldset>
                    {error && <p role="alert" className="mt-5 text-sm text-rose-300">{error}{error.startsWith('This model is not ready') && <> <button type="button" className="underline" onClick={() => onNavigate?.('model-cache')}>Open Model Cache</button></>}</p>}
                    {mode === 'advanced' && <div className="mt-7 flex justify-end"><Button type="submit" disabled={!cluster || !deployName || submitting}><Rocket size={16} aria-hidden="true" />{submitting ? 'Creating deployment...' : 'Configure deployment'}</Button></div>}
                </form>
                {mode === 'agentic' ? <aside><AgenticDeploymentWorkspace embedded draft={agenticDraft && { ...agenticDraft, storageVolumeId, vllmArguments }} vllmParameterControls={vllmArgumentControls} onBack={() => { setAgenticDraft(null); setMode('standard'); }} onDeploymentStarted={() => onNavigate('optimization-deployments')} /></aside> : mode === 'standard' && <aside className="rounded-lg border border-slate-800 bg-slate-900/70 p-5"><div className="flex items-center gap-2 text-sm font-medium text-white"><Cpu size={17} className="text-cyan-300" />Standard vLLM plan</div><dl className="mt-5 space-y-3 text-sm"><div className="flex justify-between gap-3"><dt className="text-slate-500">Recommended replicas</dt><dd className="text-slate-200">{plan.replicas}</dd></div><div className="flex justify-between gap-3"><dt className="text-slate-500">Tensor parallelism</dt><dd className="text-slate-200">TP {plan.tensorParallelism}</dd></div><div className="flex justify-between gap-3"><dt className="text-slate-500">CPU offload</dt><dd className="text-slate-200">{plan.cpuOffload ? 'Recommended' : 'Not required'}</dd></div>{plan.availableGpus > 0 && <div className="flex justify-between gap-3"><dt className="text-slate-500">Available GPU / VRAM</dt><dd className="text-right text-slate-200">{plan.availableGpus} / {plan.vramPerGpuGiB.toFixed(0)} GiB</dd></div>}</dl><section className="mt-5 border-t border-slate-800 pt-4"><button type="button" onClick={() => setVllmParametersOpen((open) => !open)} className="flex w-full items-center justify-between text-sm font-medium text-white"><span>vLLM parameters</span><ChevronDown size={16} className={`transition ${vllmParametersOpen ? 'rotate-180' : ''}`} /></button>{vllmParametersOpen && <div className="mt-3 grid gap-3"><label className="text-xs text-slate-400">GPU memory utilization<input className="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 text-sm text-slate-200" type="number" min="0.5" max="0.98" step="0.01" value={vllm.gpuMemoryUtilization} onChange={(event) => setVllm((current) => ({ ...current, gpuMemoryUtilization: Number(event.target.value) }))} /></label><label className="text-xs text-slate-400">Max model length<input className="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 text-sm text-slate-200" type="number" min="256" max="131072" step="256" value={vllm.maxModelLen ?? plan.maxModelLen} onChange={(event) => setVllm((current) => ({ ...current, maxModelLen: Number(event.target.value) }))} /></label><label className="text-xs text-slate-400">Max number of sequences<input className="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 text-sm text-slate-200" type="number" min="1" max="4096" value={vllm.maxNumSeqs} onChange={(event) => setVllm((current) => ({ ...current, maxNumSeqs: Number(event.target.value) }))} /></label><label className="text-xs text-slate-400">Max batched tokens<input className="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 text-sm text-slate-200" type="number" min="256" max="131072" step="256" value={vllm.maxNumBatchedTokens} onChange={(event) => setVllm((current) => ({ ...current, maxNumBatchedTokens: Number(event.target.value) }))} /></label><label className="flex items-center justify-between gap-3 text-xs text-slate-300"><span>Prefix caching</span><input className="h-4 w-4 accent-cyan-400" type="checkbox" checked={vllm.enablePrefixCaching} onChange={(event) => setVllm((current) => ({ ...current, enablePrefixCaching: event.target.checked }))} /></label><label className="flex items-center justify-between gap-3 text-xs text-slate-300"><span>Enforce eager execution</span><input className="h-4 w-4 accent-cyan-400" type="checkbox" checked={vllm.enforceEager} onChange={(event) => setVllm((current) => ({ ...current, enforceEager: event.target.checked }))} /></label>{vllmArgumentControls}</div>}</section><section className="mt-5 border-t border-slate-800 pt-4"><button type="button" onClick={() => setEnvironmentOpen((open) => !open)} className="flex w-full items-center justify-between text-sm font-medium text-white"><span>Deployment environment</span><ChevronDown size={16} className={`transition ${environmentOpen ? 'rotate-180' : ''}`} /></button>{environmentOpen && <div className="mt-3 grid gap-3"><label className="flex items-center justify-between gap-3 text-xs text-slate-300"><span>Use cluster proxy</span><input className="h-4 w-4 accent-cyan-400" type="checkbox" checked={useClusterProxy} onChange={(event) => setUseClusterProxy(event.target.checked)} /></label>{useClusterProxy && <><label className="text-xs text-slate-400">HTTP proxy<input className="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 text-sm text-slate-200" placeholder="Inherited from backend" value={proxy.httpProxy} onChange={(event) => setProxy((current) => ({ ...current, httpProxy: event.target.value }))} /></label><label className="text-xs text-slate-400">HTTPS proxy<input className="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 text-sm text-slate-200" placeholder="Inherited from backend" value={proxy.httpsProxy} onChange={(event) => setProxy((current) => ({ ...current, httpsProxy: event.target.value }))} /></label><label className="text-xs text-slate-400">NO_PROXY<input className="mt-1 h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 text-sm text-slate-200" placeholder="Inherited with cluster defaults" value={proxy.noProxy} onChange={(event) => setProxy((current) => ({ ...current, noProxy: event.target.value }))} /></label></>}</div>}</section><p className="mt-5 border-t border-slate-800 pt-4 text-xs leading-5 text-slate-500">Recommendations use available GPU count and VRAM. The standard flow creates a raw vLLM baseline.</p><div className="mt-4 flex items-center gap-2 text-xs text-emerald-300"><CheckCircle2 size={14} aria-hidden="true" />vLLM baseline</div><div className="mt-5 flex justify-end"><ResourceBasisControl value={resourceBasis} hardware={hardware} requiredCards={requestedGpuCount} disabled={!cluster || (requiresModelCache && (!storageVolumeId || !cacheReady || cacheCheckLoading)) || !deployName || submitting || !selected.vllmSupported} loading={submitting} label="Create deployment" onGenerate={(basis) => { setResourceBasis(basis); submit(basis); }} confirmLabel="Confirm & deploy" /></div></aside>}
            </div>
        </ModulePage>
    );
}
