import { requestJson } from '../../api/httpClient';
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

// Shared state for the Deploy Workflow. Each pipeline stage is its own Prism
// page/tab, so the workflow state (workload, run status, mocked results) lives
// here in a provider mounted above the view switch and is persisted to
// localStorage — that way navigating between stage tabs keeps context.

import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
    MOCK_MODELS, MOCK_ACCELERATORS, CANDIDATE_SOURCES,
    searchCandidates, runBenchmark, runSimulation, computeTco, recommend,
} from './mockBackend';
import { checkAicSupport, searchAicCandidates } from './candidateBackend';
import { renderConfiguration, resolveConfigurations, saveConfiguration } from './configurationBackend';
import { loadCluster } from './clusterBackend';
import { loadGuideCatalog, planGuideDeployment } from './guidePlanningBackend';
import {
    getLocalDeploymentRun, getLocalEvaluationRun, getRemoteDeploymentStatus, getRemoteDeploymentLogs, searchLocalDeploymentRuns, startLocalDeployment, startLocalEvaluation, teardownRemoteDeployment,
} from './remoteDeployBackend';
import { STAGES, WorkflowContext } from './workflowStore';

const STORAGE_KEY = 'prism_deploy_workflow_v5';
const DEPLOYMENT_ENVIRONMENT_DEFAULTS_VERSION = 3;

function canonicalJson(value) {
    if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
    if (value && typeof value === 'object') {
        return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
    }
    return JSON.stringify(value);
}

function sha256Hex(value) {
    const bytes = Array.from(new TextEncoder().encode(value));
    const bitLength = bytes.length * 8;
    bytes.push(0x80);
    while (bytes.length % 64 !== 56) bytes.push(0);
    for (let shift = 56; shift >= 0; shift -= 8) bytes.push(Math.floor(bitLength / 2 ** shift) & 0xff);

    const constants = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ];
    const hash = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    const words = new Array(64);

    for (let offset = 0; offset < bytes.length; offset += 64) {
        for (let index = 0; index < 16; index += 1) {
            const byteOffset = offset + index * 4;
            words[index] = (bytes[byteOffset] << 24) | (bytes[byteOffset + 1] << 16) | (bytes[byteOffset + 2] << 8) | bytes[byteOffset + 3];
        }
        for (let index = 16; index < 64; index += 1) {
            const s0 = ((words[index - 15] >>> 7) | (words[index - 15] << 25)) ^ ((words[index - 15] >>> 18) | (words[index - 15] << 14)) ^ (words[index - 15] >>> 3);
            const s1 = ((words[index - 2] >>> 17) | (words[index - 2] << 15)) ^ ((words[index - 2] >>> 19) | (words[index - 2] << 13)) ^ (words[index - 2] >>> 10);
            words[index] = (words[index - 16] + s0 + words[index - 7] + s1) | 0;
        }

        let [a, b, c, d, e, f, g, h] = hash;
        for (let index = 0; index < 64; index += 1) {
            const s1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
            const choice = (e & f) ^ (~e & g);
            const temp1 = (h + s1 + choice + constants[index] + words[index]) | 0;
            const s0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
            const majority = (a & b) ^ (a & c) ^ (b & c);
            const temp2 = (s0 + majority) | 0;
            [h, g, f, e, d, c, b, a] = [g, f, e, (d + temp1) | 0, c, b, a, (temp1 + temp2) | 0];
        }
        hash[0] = (hash[0] + a) | 0;
        hash[1] = (hash[1] + b) | 0;
        hash[2] = (hash[2] + c) | 0;
        hash[3] = (hash[3] + d) | 0;
        hash[4] = (hash[4] + e) | 0;
        hash[5] = (hash[5] + f) | 0;
        hash[6] = (hash[6] + g) | 0;
        hash[7] = (hash[7] + h) | 0;
    }
    return hash.map((word) => (word >>> 0).toString(16).padStart(8, '0')).join('');
}

const DEFAULT_WORKLOAD = {
    model: MOCK_MODELS[1],
    guide: 'optimized-baseline',
    accelerator: MOCK_ACCELERATORS[0],
    isl: 1024,
    osl: 256,
    concurrency: 64,
    ttftMs: 500,
    tpotMs: 25,
};

const createDeploymentConfiguration = () => ({
    id: `deployment-configuration-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    guide: 'optimized-baseline',
    model: 'Qwen/Qwen3-0.6B',
    replicas: 1,
    tensorParallelSize: 1,
    sourceMode: 'default-guide',
});

const persistedArray = (value, fallback = []) => Array.isArray(value) ? value : fallback;

const DEFAULT_REMOTE_TARGET = {
    host: '', port: 22, username: '', authMethod: 'agent', keyPath: '', hostFingerprint: '',
    cards: ['0', '1', '2', '3', { id: '4', status: 'allocated' }, { id: '5', status: 'allocated' }, { id: '6', status: 'allocated' }, { id: '7', status: 'allocated' }],
    nodeSelection: 'current-server',
};

const DEFAULT_DEPLOYMENT_ENVIRONMENT = {
    repository: '', branch: '',
    httpProxy: 'http://proxy.ims.intel.com:911', httpsProxy: 'http://proxy.ims.intel.com:911',
    noProxy: 'intel.com,.intel.com,localhost,127.0.0.1', modelSource: 'download', mountPath: '', mountModelName: '', storageVolumeId: '',
    imageMode: 'use-upstream-image', upstreamImage: 'ghcr.io/llm-d/llm-d-xpu', buildSourceUrl: '', targetImageName: '',
};

const isValidWorkload = (value) => Boolean(value.model?.trim() && value.accelerator
    && Number(value.isl) > 0 && Number(value.osl) > 0 && Number(value.concurrency) > 0);

const SOURCE_API_NAMES = {
    aic: 'aic',
    baseline: 'baseline_search',
    pd: 'pd_search',
    epd: 'epd_search',
    'tiered-cache': 'tiered_cache_search',
    historical: 'historical',
    manual: 'manual',
};

const sourcePayload = (candidate, workload, searchConfig) => {
    const type = candidate.topologyMode === 'agg' ? 'baseline'
        : candidate.topologyMode === 'disagg' ? 'pd'
            : candidate.topologyMode === 'tiered-cache' ? 'tiered_cache'
                : candidate.topologyMode || SOURCE_API_NAMES[candidate.source]?.replace('_search', '') || 'baseline';
    const payload = {
        type,
        model: workload.model,
        hardware: {
            accelerator_type: /b60|xpu|pvc|max_/i.test(searchConfig.aicSystemName) ? 'xpu' : 'cuda',
            accelerator_model: searchConfig.aicSystemName,
            accelerator_count: Number(searchConfig.totalGpus),
            aic_system_name: searchConfig.aicSystemName,
            aic_backend_name: searchConfig.aicBackendName,
        },
        max_model_len: Number(workload.isl) + Number(workload.osl),
        network: {
            protocol: 'HTTP',
            inference_pool: {
                target_port_number: 8000,
                selector: { 'llm-d.ai/inferenceServing': 'true' },
                extension_ref: { port_number: 9002, failure_mode: 'FailClose' },
            },
            http_route: {
                gateway_discovery: 'first_in_namespace',
                path_prefix: '/',
                request_timeout: '0s',
                backend_request_timeout: '0s',
            },
        },
        performance: candidate.predicted || undefined,
    };
    if (type === 'baseline' || type === 'tiered_cache') {
        payload.serving = { tp: candidate.decodeTp, replicas: candidate.decodeReplicas };
    } else {
        if (candidate.encodeTp) payload.encode = { tp: candidate.encodeTp, replicas: candidate.encodeReplicas };
        payload.prefill = { tp: candidate.prefillTp, replicas: candidate.prefillReplicas };
        payload.decode = { tp: candidate.decodeTp, replicas: candidate.decodeReplicas };
    }
    if (type === 'tiered_cache') payload.cache = { backend: 'tiered_prefix_cache', gpu_fraction: 0.8 };
    return payload;
};

const toConfigurationFile = (candidate, workload, searchConfig) => ({
    id: candidate.id,
    source: candidate.source,
    name: `${candidate.name.replace(/[^a-z0-9_.-]+/gi, '-')}.yaml`,
    inputMode: 'source_result',
    payloadText: JSON.stringify(sourcePayload(candidate, workload, searchConfig), null, 2),
    selected: true,
});

const toDeploymentCandidate = (result, rendered, saved, index) => {
    const config = result.candidate_config;
    const source = Object.entries(SOURCE_API_NAMES).find(([, value]) => value === config.candidate_source.name)?.[0]
        || config.candidate_source.name;
    const serving = config.serving || {};
    const prefill = config.prefill || {};
    const decode = config.decode || serving;
    const encode = config.encode || {};
    const totalGpus = (Number(serving.tensor_parallel_size || 0) * Number(serving.replicas || 0))
        + (Number(encode.tensor_parallel_size || 0) * Number(encode.replicas || 0))
        + (Number(prefill.tensor_parallel_size || 0) * Number(prefill.replicas || 0))
        + (Number(config.decode?.tensor_parallel_size || 0) * Number(config.decode?.replicas || 0));
    const sourceMeta = CANDIDATE_SOURCES.find((item) => item.id === source);
    return {
        id: result.configuration_id,
        name: saved.configuration_file.file_name || `configuration-${index + 1}`,
        source,
        sourceLabel: sourceMeta?.label || source,
        family: sourceMeta?.family || 'Existing / User',
        topologyMode: config.type,
        prefillTp: Number(prefill.tensor_parallel_size || 0),
        prefillReplicas: Number(prefill.replicas || 0),
        decodeTp: Number(decode.tensor_parallel_size || 1),
        decodeReplicas: Number(decode.replicas || 1),
        encodeTp: encode.tensor_parallel_size ? Number(encode.tensor_parallel_size) : null,
        encodeReplicas: encode.replicas ? Number(encode.replicas) : null,
        totalGpus,
        predicted: config.performance || null,
        confidence: null,
        deployable: result.validation.status !== 'invalid',
        validation: result.validation,
        candidateConfig: config,
        deployableConfiguration: rendered.deployable_configuration,
        configurationFile: saved.configuration_file,
    };
};

const loadPersisted = () => {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        return raw ? JSON.parse(raw) : {};
    } catch {
        return {};
    }
};

export function WorkflowProvider({ children }) {
    const persisted = loadPersisted();

    const initialWorkload = persisted.workload || DEFAULT_WORKLOAD;
    const [workload, setWorkloadState] = useState(initialWorkload);
    const [status, setStatus] = useState({
        ...(persisted.status || {}),
        'define-workload': isValidWorkload(initialWorkload) ? 'done' : 'idle',
    });
    const [candidateSourceIds, setCandidateSourceIds] = useState(() => persistedArray(persisted.candidateSourceIds));
    const [candidateSearchConfig, setCandidateSearchConfig] = useState({
        totalGpus: 8, aicSystemName: 'b60', aicBackendName: 'vllm', aicDatabaseMode: 'SILICON',
        maxCandidatesPerMode: 3, ...(persisted.candidateSearchConfig || {}),
    });
    const [guideCatalog, setGuideCatalog] = useState(null);
    const [guideSelection, setGuideSelection] = useState({
        guide: 'optimized-baseline', accelerator: 'gpu', modelServer: 'vllm',
        ...(persisted.guideSelection || {}),
    });
    const [guidePlanningPhase, setGuidePlanningPhase] = useState('idle');
    const [guidePlanningError, setGuidePlanningError] = useState('');
    const [planningResult, setPlanningResult] = useState(persisted.planningResult || null);
    const [manualCandidate, setManualCandidate] = useState({
        inputMode: 'form', name: 'manual-pd-config', servingMode: 'disagg',
        tp: 1, pp: 1, replicas: 1, batchSize: 128,
        prefillTp: 1, prefillReplicas: 1, prefillBatchSize: 1,
        decodeTp: 1, decodeReplicas: 2, decodeBatchSize: 64,
        yamlText: '', yamlFilename: '', ...(persisted.manualCandidate || {}),
    });
    const [candidates, setCandidates] = useState(() => Array.isArray(persisted.candidates) ? persisted.candidates : null);
    const [sourceConfigurations, setSourceConfigurations] = useState(() => persistedArray(persisted.sourceConfigurations));
    const [configurationPhase, setConfigurationPhase] = useState('idle');
    const configurationLoadId = useRef(0);
    const initialConfigurationLoadStarted = useRef(false);
    const [selectedCandidateIds, setSelectedCandidateIds] = useState(() => persistedArray(persisted.selectedCandidateIds));
    const [candidateSearchError, setCandidateSearchError] = useState('');
    const [candidateSupport, setCandidateSupport] = useState(null);
    const [candidateSupportError, setCandidateSupportError] = useState('');
    const [checkingCandidateSupport, setCheckingCandidateSupport] = useState(false);
    const [deployConfig, setDeployConfig] = useState({
        name: 'llm-d-service', namespace: 'llm-d-optimized', machineType: 'xeon6-xpu-8',
        gpusPerNode: 8, replicas: 2, ...(persisted.deployConfig || {}),
        cluster: 'remote-ssh', nodes: 1,
        accelerator: /^Intel Data Center GPU/.test(persisted.deployConfig?.accelerator || '')
            ? persisted.deployConfig.accelerator
            : 'Intel Data Center GPU Max 1550',
    });
    const [deploymentConfigurations, setDeploymentConfigurations] = useState(() => {
        const configurations = persistedArray(persisted.deploymentConfigurations);
        return configurations.length ? configurations : [createDeploymentConfiguration()];
    });
    const [remoteTarget, setRemoteTarget] = useState(() => {
        const savedTarget = persisted.remoteTarget || {};
        const cards = savedTarget.cards?.length === 1 && savedTarget.cards[0] === '0'
            ? DEFAULT_REMOTE_TARGET.cards
            : savedTarget.cards || DEFAULT_REMOTE_TARGET.cards;
        return { ...DEFAULT_REMOTE_TARGET, ...savedTarget, cards };
    });
    const [deploymentEnvironment, setDeploymentEnvironment] = useState({
        ...DEFAULT_DEPLOYMENT_ENVIRONMENT, ...(persisted.deploymentEnvironment || {}),
    });
    const [deployments, setDeployments] = useState(() => persistedArray(persisted.deployments));
    const [activeDeploymentId, setActiveDeploymentId] = useState(persisted.activeDeploymentId || '');
    const [benchmarkRuns, setBenchmarkRuns] = useState(() => persistedArray(persisted.benchmarkRuns));
    const [simulationRuns, setSimulationRuns] = useState(() => persistedArray(persisted.simulationRuns));
    const [selectedBenchmarkIds, setSelectedBenchmarkIds] = useState(() => persistedArray(persisted.selectedBenchmarkIds));
    const [tco, setTco] = useState(persisted.tco || null);
    const [rec, setRec] = useState(persisted.rec || null);
    const [deploymentError, setDeploymentError] = useState('');
    const [localDeploymentRun, setLocalDeploymentRun] = useState(persisted.localDeploymentRun || null);
    const setStep = useCallback((id, s) => setStatus((prev) => ({ ...prev, [id]: s })), []);

    useEffect(() => {
        let cancelled = false;
        requestJson('/api/config', {}, { strictJson: true, errorFactory: () => new Error('Configuration unavailable') }).then((config) => {
            if (cancelled) return;
            const defaults = config.deploymentDefaults || {};
            setDeploymentEnvironment((current) => Object.fromEntries(Object.entries({ ...current, ...defaults })
                .map(([field, value]) => {
                    const keepCurrent = ['repository', 'branch'].includes(field)
                        || persisted.deploymentEnvironmentDefaultsVersion === DEPLOYMENT_ENVIRONMENT_DEFAULTS_VERSION
                        || !value;
                    return [field, keepCurrent ? current[field] : value];
                })));
        }).catch(() => {
            // Keep built-in defaults when the general configuration service is unavailable.
        });
        return () => { cancelled = true; };
    }, []);

    useEffect(() => {
        const clusterId = sessionStorage.getItem('prism_cluster_server_id');
        if (!clusterId) return undefined;
        let cancelled = false;
        loadCluster(clusterId).then((cluster) => {
            if (cancelled) return;
            setDeploymentEnvironment((current) => ({
                ...current,
                repository: cluster.llmDRepoPath || '',
                branch: cluster.llmDRef || '',
            }));
        }).catch(() => {
            // Deployment performs the authoritative lookup and reports a useful
            // error if this cluster has no downloaded source checkout.
        });
        return () => { cancelled = true; };
    }, []);

    useEffect(() => {
        const activeRuns = benchmarkRuns.filter((run) => ['queued', 'running'].includes(run.status));
        if (!activeRuns.length) return undefined;
        let cancelled = false;
        const refresh = async () => {
            const updates = await Promise.all(activeRuns.map(async (run) => {
                try {
                    const evaluation = await getLocalEvaluationRun(run.id);
                    const metrics = evaluation.metrics || {};
                    return {
                        ...run,
                        status: evaluation.status,
                        output: evaluation.output,
                        measured: {
                            ttftMs: metrics.ttft_ms,
                            tpotMs: metrics.tpot_ms,
                            throughputTps: metrics.throughput_tps,
                            errorRate: evaluation.status === 'failed' ? 100 : 0,
                        },
                    };
                } catch {
                    return run;
                }
            }));
            if (!cancelled) {
                setBenchmarkRuns((current) => current.map((run) => updates.find((update) => update.id === run.id) || run));
                if (updates.some((run) => run.status === 'succeeded')) setStep('benchmark', 'done');
            }
        };
        refresh();
        const timer = window.setInterval(refresh, 5_000);
        return () => { cancelled = true; window.clearInterval(timer); };
    }, [benchmarkRuns, setStep]);

    useEffect(() => {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify({
                workload, status, candidateSourceIds, candidateSearchConfig, manualCandidate, sourceConfigurations, candidates, selectedCandidateIds,
                guideSelection, planningResult,
                deployConfig, deploymentConfigurations, remoteTarget, deploymentEnvironment,
                deploymentEnvironmentDefaultsVersion: DEPLOYMENT_ENVIRONMENT_DEFAULTS_VERSION,
                deployments, activeDeploymentId, localDeploymentRun, benchmarkRuns, simulationRuns,
                selectedBenchmarkIds, tco, rec,
            }));
        } catch {
            // Ignore quota and serialization errors.
        }
    }, [workload, status, candidateSourceIds, candidateSearchConfig, manualCandidate, sourceConfigurations, candidates, selectedCandidateIds,
        guideSelection, planningResult, deploymentConfigurations,
        deployConfig, deployments, activeDeploymentId, localDeploymentRun, benchmarkRuns, simulationRuns,
        selectedBenchmarkIds, tco, rec, remoteTarget, deploymentEnvironment]);

    useEffect(() => {
        if (!localDeploymentRun) return undefined;
        let cancelled = false;
        const refresh = async () => {
            try {
                const run = await getLocalDeploymentRun(localDeploymentRun.id);
                if (!cancelled) setLocalDeploymentRun(run);
            } catch (error) {
                if (!cancelled) setDeploymentError(error instanceof Error ? error.message : 'Unable to load deployment status');
            }
        };
        refresh();
        const timer = window.setInterval(refresh, 5 * 60 * 1000);
        return () => {
            cancelled = true;
            window.clearInterval(timer);
        };
    }, [localDeploymentRun?.id]);

    useEffect(() => {
        let cancelled = false;
        const synchronizeLatestRun = () => {
            searchLocalDeploymentRuns('').then((runs) => {
                if (!cancelled && runs[0]) setLocalDeploymentRun(runs[0]);
            }).catch(() => {
                // Keep the persisted run when the local Deploy API is unavailable.
            });
        };
        synchronizeLatestRun();
        const timer = window.setInterval(synchronizeLatestRun, 30_000);
        return () => {
            cancelled = true;
            window.clearInterval(timer);
        };
    }, []);
    const addDeploymentConfiguration = useCallback(() => {
        setDeploymentConfigurations((previous) => [...previous, createDeploymentConfiguration()]);
    }, []);
    const updateDeploymentConfiguration = useCallback((id, updates) => {
        setDeploymentConfigurations((previous) => previous.map((configuration) => configuration.id === id ? { ...configuration, ...updates } : configuration));
    }, []);
    const removeDeploymentConfiguration = useCallback((id) => {
        setDeploymentConfigurations((previous) => previous.length > 1 ? previous.filter((configuration) => configuration.id !== id) : previous);
    }, []);
    const setWorkload = useCallback((nextWorkload) => {
        setWorkloadState(nextWorkload);
        setStatus((previous) => ({
            ...previous,
            'define-workload': isValidWorkload(nextWorkload) ? 'done' : 'idle',
        }));
    }, []);
    const st = useCallback((id) => status[id] || 'idle', [status]);
    const sla = useMemo(() => ({ ttftMs: Number(workload.ttftMs), tpotMs: Number(workload.tpotMs) }), [workload]);

    const progressPct = useMemo(() => {
        const done = STAGES.filter((s) => status[s.id] === 'done').length;
        return Math.round((done / STAGES.length) * 100);
    }, [status]);

    useEffect(() => {
        let active = true;
        setGuidePlanningPhase('catalog');
        loadGuideCatalog()
            .then((catalog) => {
                if (!active) return;
                setGuideCatalog(catalog);
                const guides = persistedArray(catalog.guides);
                const selectedGuide = guides.find((item) => item.id === guideSelection.guide) || guides[0];
                const accelerators = persistedArray(selectedGuide?.accelerators);
                const selectedAccelerator = accelerators.find((item) => item.id === guideSelection.accelerator) || accelerators[0];
                const modelServers = persistedArray(selectedAccelerator?.modelServers);
                const selectedModelServer = modelServers.find((item) => item.id === guideSelection.modelServer) || modelServers[0];
                if (selectedGuide && selectedAccelerator && selectedModelServer) {
                    setGuideSelection({ guide: selectedGuide.id, accelerator: selectedAccelerator.id, modelServer: selectedModelServer.id });
                }
                setGuidePlanningPhase('ready');
            })
            .catch((error) => {
                if (!active) return;
                setGuidePlanningError(error instanceof Error ? error.message : 'Could not load guide catalog');
                setGuidePlanningPhase('idle');
            });
        return () => { active = false; };
    // The catalog is loaded once; selection changes are handled by the UI.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const handlePlanGuideDeployment = useCallback(async (environment = { mode: 'current' }) => {
        setGuidePlanningPhase('planning');
        setGuidePlanningError('');
        setStep('search-candidates', 'running');
        try {
            const result = await planGuideDeployment({ ...guideSelection, model: workload.model, environment });
            const summary = result.summary || {};
            const plannedCandidate = {
                id: `guide-${guideSelection.guide}-${guideSelection.accelerator}-${guideSelection.modelServer}`,
                name: `${guideSelection.guide}-${guideSelection.accelerator}-${guideSelection.modelServer}.yaml`,
                source: 'official-guide',
                sourceLabel: 'Official llm-d Guide',
                family: 'Official Guide',
                topologyMode: guideSelection.guide,
                prefillTp: 0,
                prefillReplicas: 0,
                decodeTp: Number(summary.tensorParallelSize || 1),
                decodeReplicas: Number(summary.plannedReplicas || 1),
                totalGpus: Number(summary.acceleratorsPerReplica || summary.tensorParallelSize || 1) * Number(summary.plannedReplicas || 1),
                predicted: null,
                deployable: result.validation?.status !== 'invalid',
                validation: result.validation,
                candidateConfig: { guide: result.source, model: result.modelConfig, planningPatch: result.planningPatch },
                deployableConfiguration: result.plannedDeployment,
                configurationFile: null,
            };
            setPlanningResult(result);
            setCandidates([plannedCandidate]);
            setSelectedCandidateIds(plannedCandidate.deployable ? [plannedCandidate.id] : []);
            setGuidePlanningPhase('done');
            setStep('search-candidates', 'done');
            return result;
        } catch (error) {
            setGuidePlanningError(error instanceof Error ? error.message : 'Guide planning failed');
            setGuidePlanningPhase('ready');
            setStep('search-candidates', 'idle');
            return null;
        }
    }, [guideSelection, workload.model, setStep]);

    const handleUpdatePlannedYaml = useCallback((content) => {
        setPlanningResult((previous) => previous ? {
            ...previous,
            plannedDeployment: { ...previous.plannedDeployment, content },
        } : previous);
        setCandidates((previous) => previous?.map((candidate) => candidate.source === 'official-guide' ? {
            ...candidate,
            deployableConfiguration: { ...candidate.deployableConfiguration, content },
        } : candidate));
    }, []);

    const handleCheckCandidateSupport = useCallback(async () => {
        setCheckingCandidateSupport(true);
        setCandidateSupportError('');
        setCandidateSupport(null);
        try {
            const result = await checkAicSupport(workload, candidateSearchConfig);
            setCandidateSupport(result);
            return result;
        } catch (error) {
            setCandidateSupportError(error instanceof Error ? error.message : 'Support check failed');
            return null;
        } finally {
            setCheckingCandidateSupport(false);
        }
    }, [workload, candidateSearchConfig]);

    const handleLoadConfigurations = useCallback(async (requestedSourceIds = candidateSourceIds) => {
        const loadId = configurationLoadId.current + 1;
        configurationLoadId.current = loadId;
        if (!requestedSourceIds.length) {
            setSourceConfigurations([]);
            setCandidates(null);
            setSelectedCandidateIds([]);
            setCandidateSearchError('');
            setConfigurationPhase('idle');
            setStep('search-candidates', 'idle');
            return;
        }
        setStep('search-candidates', 'running');
        setConfigurationPhase('loading');
        setCandidateSearchError('');
        try {
            const generatedSourceIds = requestedSourceIds.filter((source) => source !== 'historical' && source !== 'manual');
            const loadSourceIds = [...generatedSourceIds];
            const sourceLoads = generatedSourceIds.map(async (source) => ({
                source,
                candidates: await searchAicCandidates(workload, [source], candidateSearchConfig, manualCandidate),
            }));
            if (requestedSourceIds.includes('historical')) {
                loadSourceIds.push('historical');
                sourceLoads.push(searchCandidates(workload, ['historical'], manualCandidate)
                    .then((sourceCandidates) => ({ source: 'historical', candidates: sourceCandidates })));
            }
            const settledLoads = await Promise.allSettled(sourceLoads);
            if (loadId !== configurationLoadId.current) return;
            const loadedCandidates = settledLoads
                .filter((result) => result.status === 'fulfilled')
                .flatMap((result) => result.value.candidates);
            const loadErrors = settledLoads
                .map((result, index) => result.status === 'rejected'
                    ? `${CANDIDATE_SOURCES.find((item) => item.id === loadSourceIds[index])?.label || loadSourceIds[index] || 'Source'}: ${result.reason instanceof Error ? result.reason.message : 'load failed'}`
                    : null)
                .filter(Boolean);
            const firstConfigurationBySource = new Set();
            const files = loadedCandidates.map((candidate) => {
                const file = toConfigurationFile(candidate, workload, candidateSearchConfig);
                file.selected = !firstConfigurationBySource.has(candidate.source);
                firstConfigurationBySource.add(candidate.source);
                return file;
            });
            if (requestedSourceIds.includes('manual')) {
                const manualPayload = manualCandidate.inputMode === 'yaml' ? manualCandidate.yamlText : JSON.stringify({
                    type: manualCandidate.servingMode === 'agg' ? 'baseline' : 'pd',
                    model: workload.model,
                    hardware: {
                        accelerator_type: /b60|xpu|pvc|max_/i.test(candidateSearchConfig.aicSystemName) ? 'xpu' : 'cuda',
                        accelerator_model: candidateSearchConfig.aicSystemName,
                        accelerator_count: Number(candidateSearchConfig.totalGpus),
                        aic_system_name: candidateSearchConfig.aicSystemName,
                        aic_backend_name: candidateSearchConfig.aicBackendName,
                    },
                    serving: manualCandidate.servingMode === 'agg' ? { tp: Number(manualCandidate.tp), replicas: Number(manualCandidate.replicas) } : undefined,
                    prefill: manualCandidate.servingMode !== 'agg' ? { tp: Number(manualCandidate.prefillTp), replicas: Number(manualCandidate.prefillReplicas), batch_size: Number(manualCandidate.prefillBatchSize) } : undefined,
                    decode: manualCandidate.servingMode !== 'agg' ? { tp: Number(manualCandidate.decodeTp), replicas: Number(manualCandidate.decodeReplicas), batch_size: Number(manualCandidate.decodeBatchSize) } : undefined,
                    max_model_len: Number(workload.isl) + Number(workload.osl),
                }, null, 2);
                if (!manualPayload?.trim()) throw new Error('Upload or enter a manual configuration first');
                files.push({
                    id: `manual-${Date.now()}`,
                    source: 'manual',
                    name: manualCandidate.yamlFilename || `${manualCandidate.name || 'manual-configuration'}.yaml`,
                    inputMode: manualCandidate.inputMode,
                    payloadText: manualPayload,
                    selected: true,
                });
            }
            setSourceConfigurations(files);
            setCandidates(null);
            setSelectedCandidateIds([]);
            setCandidateSearchError(loadErrors.length ? `No configuration files are available from: ${loadErrors.join('; ')}` : '');
            setConfigurationPhase('ready');
            setStep('search-candidates', 'idle');
        } catch (error) {
            if (loadId !== configurationLoadId.current) return;
            setCandidateSearchError(error instanceof Error ? error.message : 'Candidate search failed');
            setConfigurationPhase('idle');
            setStep('search-candidates', 'idle');
        }
    }, [workload, candidateSourceIds, candidateSearchConfig, manualCandidate, setStep]);

    const handleToggleCandidateSource = useCallback((source) => {
        const nextSources = candidateSourceIds.includes(source)
            ? candidateSourceIds.filter((item) => item !== source)
            : [...candidateSourceIds, source];
        setCandidateSourceIds(nextSources);
        handleLoadConfigurations(nextSources);
    }, [candidateSourceIds, handleLoadConfigurations]);

    useEffect(() => {
        if (initialConfigurationLoadStarted.current) return;
        initialConfigurationLoadStarted.current = true;
        handleLoadConfigurations(candidateSourceIds);
    }, [candidateSourceIds, handleLoadConfigurations]);

    const updateSourceConfiguration = useCallback((id, updates) => {
        setSourceConfigurations((previous) => previous.map((item) => item.id === id ? { ...item, ...updates } : item));
    }, []);

    const handleGenerateConfigurations = useCallback(async () => {
        const selected = sourceConfigurations.filter((item) => item.selected);
        if (!selected.length) return;
        setStep('search-candidates', 'running');
        setConfigurationPhase('generating');
        setCandidateSearchError('');
        try {
            const groups = selected.reduce((accumulator, item) => {
                (accumulator[item.source] ||= []).push(item);
                return accumulator;
            }, {});
            const resolved = [];
            for (const [source, configurations] of Object.entries(groups)) {
                const response = await resolveConfigurations({
                    candidate_source: { name: SOURCE_API_NAMES[source], run_id: `prism-${Date.now()}` },
                    configurations: configurations.map((item) => ({
                        configuration_id: item.id,
                        result_id: item.id,
                        input_mode: item.inputMode,
                        payload: item.payloadText,
                    })),
                    target: {},
                });
                resolved.push(...response.results);
            }
            const invalid = resolved.filter((item) => item.validation.status === 'invalid');
            if (invalid.length) {
                const details = invalid.flatMap((item) => item.validation.errors.map((error) => `${item.configuration_id}: ${error}`));
                throw new Error(details.join('; '));
            }
            const generated = await Promise.all(resolved.map(async (result, index) => {
                const rendered = await renderConfiguration(result.candidate_config, {
                    guide_ref: `${result.candidate_config.type}-deployment`,
                    template_ref: `templates/${result.candidate_config.type}`,
                });
                const baseName = sourceConfigurations.find((item) => item.id === result.configuration_id)?.name
                    || `${result.candidate_config.type}-${index + 1}.yaml`;
                const saved = await saveConfiguration(rendered.deployable_configuration, baseName);
                return toDeploymentCandidate(result, rendered, saved, index);
            }));
            setCandidates(generated);
            setSelectedCandidateIds(generated.map((candidate) => candidate.id));
            setConfigurationPhase('done');
            setStep('search-candidates', 'done');
        } catch (error) {
            setCandidateSearchError(error instanceof Error ? error.message : 'Configuration generation failed');
            setConfigurationPhase('ready');
            setStep('search-candidates', 'idle');
        }
    }, [sourceConfigurations, setStep]);

    const handleDeploy = useCallback(async () => {
        setDeploymentError('');
        setStep('deployment', 'running');
        try {
            const plans = deploymentConfigurations.map((configuration) => ({
                ...configuration,
                replicas: Number(configuration.replicas),
                tensorParallelSize: Number(configuration.tensorParallelSize),
            }));
            if (!plans.length) {
                throw new Error('Select at least one provided configuration before deployment');
            }
            if (plans.some((configuration) => configuration.sourceMode === 'upload-file' && (!configuration.sourceFileName || !configuration.uploadedYaml))) {
                throw new Error('Upload a local YAML file before deployment');
            }
            if (plans.some((configuration) => !configuration.guide.trim() || !configuration.model.trim() || !Number.isInteger(configuration.replicas) || configuration.replicas < 1 || !Number.isInteger(configuration.tensorParallelSize) || configuration.tensorParallelSize < 1)) {
                throw new Error('Every configuration requires a guide, model, replicas, and TP greater than zero');
            }
            const clusterId = sessionStorage.getItem('prism_cluster_server_id');
            if (!clusterId) throw new Error('Select a cluster before deployment');
            const sourceCluster = await loadCluster(clusterId);
            if (!deploymentEnvironment.httpProxy || !deploymentEnvironment.httpsProxy || !deploymentEnvironment.noProxy) {
                throw new Error('Model runtime environment is required');
            }
            const useMountedModel = Boolean(
                !deploymentEnvironment.storageVolumeId && deploymentEnvironment.mountPath && deploymentEnvironment.mountModelName
            );
            const useStorageVolume = Boolean(deploymentEnvironment.storageVolumeId);
            const image = deploymentEnvironment.imageMode === 'build-from-source'
                ? deploymentEnvironment.targetImageName
                : deploymentEnvironment.upstreamImage;
            if (!image) throw new Error('Upstream image is required');
            if (deploymentEnvironment.imageMode === 'build-from-source' && !deploymentEnvironment.buildSourceUrl) {
                throw new Error('Build image from source requires a source URL and target image name');
            }
            const configurations = plans.map((configuration) => {
                const content = {
                    model: { name: configuration.model },
                    decode: {
                        replicaCount: configuration.replicas,
                        tensorParallelSize: configuration.tensorParallelSize,
                    },
                    runtime: {
                        image,
                        imageMode: deploymentEnvironment.imageMode,
                        ...(configuration.sourceMode === 'suggested-deploy-yaml'
                            ? { configurationSource: { mode: 'suggested-yaml', filePath: configuration.sourceFilePath } }
                            : {}),
                        ...(configuration.sourceMode === 'upload-file'
                            ? { configurationSource: { mode: 'upload-yaml', fileName: configuration.sourceFileName, content: configuration.uploadedYaml } }
                            : {}),
                        ...(useMountedModel
                            ? { mountPath: deploymentEnvironment.mountPath, mountModelName: deploymentEnvironment.mountModelName }
                            : {}),
                        ...(useStorageVolume
                            ? { storageVolumeId: deploymentEnvironment.storageVolumeId }
                            : {}),
                        ...(deploymentEnvironment.imageMode === 'build-from-source'
                            ? { buildSourceUrl: deploymentEnvironment.buildSourceUrl }
                            : {}),
                    },
                };
                const canonical = canonicalJson(content);
                return {
                    schema_version: 'deployable-configuration.v1',
                    type: configuration.guide,
                    provider_ref: configuration.guide,
                    format: 'helm',
                    content,
                    checksum: `sha256:${sha256Hex(canonical)}`,
                };
            });
            const run = await startLocalDeployment(configurations, {
                deployment_source: {
                    ...deploymentEnvironment,
                    repository: sourceCluster.llmDRepoPath,
                    branch: sourceCluster.llmDRef || '',
                    resolved_repository: sourceCluster.llmDRepoPath,
                    benchmark_repository: sourceCluster.llmDBenchmarkRepoPath || undefined,
                    benchmark_ref: sourceCluster.llmDBenchmarkRef || undefined,
                },
                cluster_node: remoteTarget.nodeSelection,
                cluster_session_id: sessionStorage.getItem('prism_cluster_session_id') || undefined,
                cluster_server_id: clusterId,
            });
            setLocalDeploymentRun(run);
            setStep('deployment', 'done');
            return run;
        } catch (error) {
            setStep('deployment', 'idle');
            setDeploymentError(error instanceof Error ? error.message : 'Unable to start deployment');
        }
    }, [deploymentConfigurations, deployConfig, deploymentEnvironment, remoteTarget, setStep]);

    const refreshRemoteDeployment = useCallback(async (deployment, password = '') => {
        const live = await getRemoteDeploymentStatus(deployment.namespace, remoteTarget, password);
        setDeployments((previous) => previous.map((item) => item.id === deployment.id
            ? { ...item, ...live, lastCheckedAt: new Date().toISOString() }
            : item));
        return live;
    }, [remoteTarget]);

    const loadRemoteDeploymentLogs = useCallback(async (deployment, password = '', pod = '') => (
        getRemoteDeploymentLogs(deployment.namespace, remoteTarget, password, pod, 200)
    ), [remoteTarget]);

    const teardownRemote = useCallback(async (deployment, password = '') => {
        const result = await teardownRemoteDeployment(deployment.namespace, remoteTarget, password);
        setDeployments((previous) => previous.map((item) => item.id === deployment.id
            ? { ...item, status: 'deleted', endpoint: null, pods: [], logTail: result.logTail }
            : item));
        return result;
    }, [remoteTarget]);

    const handleBenchmark = useCallback(async () => {
        const readyCase = localDeploymentRun?.cases?.find((item) => item.status === 'ready' && item.execution_id);
        if (readyCase) {
            setStep('benchmark', 'running');
            try {
                const evaluation = await startLocalEvaluation(readyCase.execution_id);
                const result = {
                    id: evaluation.id,
                    name: `llm-d-benchmark ${evaluation.workload}`,
                    deploymentId: readyCase.execution_id,
                    deploymentName: evaluation.deployment_execution_id,
                    createdAt: evaluation.created_at,
                    measured: {},
                    totalGpus: 0,
                    status: evaluation.status,
                    output: null,
                };
                setBenchmarkRuns((previous) => [result, ...previous]);
                setSelectedBenchmarkIds([result.id]);
                return;
            } catch (error) {
                setStep('benchmark', 'idle');
                setDeploymentError(error instanceof Error ? error.message : 'Unable to run benchmark');
                return;
            }
        }
        const deployment = deployments.find((item) => item.id === activeDeploymentId);
        if (!deployment) return;
        setStep('benchmark', 'running');
        const deployedCandidate = candidates?.find((candidate) => candidate.id === deployment.candidateId);
        const selected = deployedCandidate ? [{
            ...deployedCandidate,
            predicted: deployedCandidate.predicted || { ttftMs: 180, tpotMs: 18, throughputTps: 2100 },
        }] : [{
            id: deployment.candidateId || deployment.id,
            name: deployment.candidateName,
            totalGpus: deployment.nodes * deployment.gpusPerNode,
            predicted: { ttftMs: 180, tpotMs: 18, throughputTps: 2100 },
        }];
        const results = await runBenchmark(deployment, selected);
        setBenchmarkRuns((previous) => [...results, ...previous]);
        setSelectedBenchmarkIds(results.map((result) => result.id));
        setStep('benchmark', 'done');
    }, [localDeploymentRun, deployments, activeDeploymentId, candidates, setStep]);

    const handleSimulation = useCallback(async () => {
        const deployment = deployments.find((item) => item.id === activeDeploymentId);
        if (!deployment) return;
        setStep('simulation', 'running');
        const result = await runSimulation(deployment, workload);
        setSimulationRuns((previous) => [result, ...previous]);
        setStep('simulation', 'done');
    }, [deployments, activeDeploymentId, workload, setStep]);

    const handleAnalyze = useCallback(async () => {
        const selected = benchmarkRuns.filter((run) => selectedBenchmarkIds.includes(run.id));
        if (!selected.length) return;
        setStep('performance-tco', 'running');
        const rows = await computeTco(selected, workload.accelerator);
        const recommendation = await recommend(rows, sla);
        setTco(rows);
        setRec(recommendation);
        setStep('performance-tco', 'done');
    }, [benchmarkRuns, selectedBenchmarkIds, workload.accelerator, sla, setStep]);

    const value = {
        workload, setWorkload,
        status, st, setStep, sla, progressPct,
        candidateSourceIds, setCandidateSourceIds, handleToggleCandidateSource, candidateSearchConfig, setCandidateSearchConfig,
        guideCatalog, guideSelection, setGuideSelection, guidePlanningPhase, guidePlanningError, planningResult, handlePlanGuideDeployment, handleUpdatePlannedYaml,
        manualCandidate, setManualCandidate, candidateSearchError,
        candidateSupport, candidateSupportError, checkingCandidateSupport, handleCheckCandidateSupport,
        sourceConfigurations, updateSourceConfiguration, configurationPhase,
        candidates, selectedCandidateIds, setSelectedCandidateIds,
        deployConfig, setDeployConfig, deploymentConfigurations, setDeploymentConfigurations, addDeploymentConfiguration, updateDeploymentConfiguration, removeDeploymentConfiguration,
        remoteTarget, setRemoteTarget, deploymentEnvironment, setDeploymentEnvironment,
        deployments, activeDeploymentId, setActiveDeploymentId, deploymentError, localDeploymentRun,
        benchmarkRuns, simulationRuns, selectedBenchmarkIds, setSelectedBenchmarkIds, tco, rec,
        handleLoadConfigurations, handleGenerateConfigurations, handleDeploy, handleBenchmark, handleSimulation, handleAnalyze,
        refreshRemoteDeployment, loadRemoteDeploymentLogs, teardownRemote,
    };

    return <WorkflowContext.Provider value={value}>{children}</WorkflowContext.Provider>;
}
