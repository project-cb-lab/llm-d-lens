import { useClipboard } from '../../hooks/useClipboard';
import { confirmDelete } from "../ui/confirmDelete";
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

// Deploy Workflow — each pipeline stage is its own Prism page/tab. The left app
// navigation IS the pipeline; walking the deploy flow means navigating between
// these stage tabs, each guiding to the next. Shared state lives in
// WorkflowContext; missing backends are mocked (see mockBackend.js).

import React from 'react';
import yaml from 'js-yaml';
import {
    Target, Search, FlaskConical, DollarSign, Sparkles, Rocket,
    CheckCircle2, Circle, Loader2, ArrowRight, ArrowLeft, Cpu, ChevronRight, ChevronDown,
    Database, WandSparkles, History, Upload, FileText, Code2, Pencil, Eye, Copy, Check,
    KeyRound, Network, ShieldCheck,
    RefreshCw, X,
} from 'lucide-react';
import { WellLitHeader, Panel, Button, Badge, StatusChip, Input, Select, Label } from '../ui';
import { cn } from '../../utils/cn';
import { MOCK_MODELS, MOCK_ACCELERATORS, MOCK_MACHINE_TYPES, MOCK_REGISTRY, CANDIDATE_SOURCES } from './mockBackend';
import { useWorkflow, STAGES, stageByView } from './workflowStore';
import RemoteDeploymentTarget from './RemoteDeploymentTarget';
import { cleanLocalDeploymentCase, fetchHostFingerprint, getLocalDeploymentCase, getLocalDeploymentRun, refreshLocalDeploymentCase, restartLocalDeploymentCase, searchLocalDeploymentRuns, stopLocalDeploymentCase } from './remoteDeployBackend';
import { listStorageVolumes } from '../StorageManagement/storageManagementBackend';

const PROVIDED_SERVICE_CONFIGURATIONS = [
    { id: 'optimized-baseline-replica-1-tp-1', guide: 'optimized-baseline', model: 'Qwen/Qwen3-0.6B', replicas: 1, tensorParallelSize: 1 },
    { id: 'optimized-baseline-replica-2-tp-2', guide: 'optimized-baseline', model: 'Qwen/Qwen3-0.6B', replicas: 2, tensorParallelSize: 2 },
];

const SUGGESTED_DEPLOY_YAML = {
    filePath: 'llm-d/guides/optimized-baseline/modelserver/xpu/vllm/patch-vllm.yaml',
};

function deploymentConfigurationFromFile(fileName, text) {
    const document = yaml.load(text);
    const configuration = document?.deployable_configuration || document;
    const content = configuration?.content || configuration;
    const model = typeof content?.model === 'string' ? content.model : content?.model?.name;
    const decode = content?.decode || content?.serving;
    const replicas = Number(decode?.replicaCount ?? decode?.replicas);
    const tensorParallelSize = Number(decode?.tensorParallelSize ?? decode?.tensor_parallel_size ?? decode?.tp);
    if (!model || !Number.isInteger(replicas) || replicas < 1 || !Number.isInteger(tensorParallelSize) || tensorParallelSize < 1) {
        throw new Error('The file must define model and decode replicas/TP values');
    }
    return {
        guide: configuration?.provider_ref || configuration?.type || 'optimized-baseline',
        model,
        replicas,
        tensorParallelSize,
        sourceMode: 'upload-file',
        sourceFileName: fileName,
    };
}

const STAGE_ICON = {
    'define-workload': Target,
    'search-candidates': Search,
    deployment: Rocket,
    benchmark: FlaskConical,
    'performance-tco': DollarSign,
};

// "What's faked here" chip, driven by MOCK_REGISTRY so mocks are explicit.
function MockChip({ stepId }) {
    const meta = MOCK_REGISTRY[stepId];
    if (!meta) return null;
    const tone = meta.level === 'real' ? 'success' : meta.level === 'mock' ? 'warning' : meta.level === 'external' || meta.level === 'hybrid' ? 'violet' : 'info';
    const label = meta.level === 'real' ? 'Real backend' : meta.level === 'mock' ? 'Mocked' : meta.level === 'external' ? 'External tab' : meta.level === 'hybrid' ? 'Hybrid' : 'Local only';
    return (
        <span title={`${meta.note}\n\nReal backend: ${meta.realBackend}`}>
            <Badge tone={tone} size="sm" className="cursor-help font-mono">{label}</Badge>
        </span>
    );
}

function StatusText({ status }) {
    const Icon = status === 'done' ? CheckCircle2 : status === 'running' ? Loader2 : Circle;
    return (
        <span className={cn('flex items-center gap-1.5 text-xs font-medium shrink-0',
            status === 'done' ? 'text-emerald-400' : status === 'running' ? 'text-blue-400' : 'text-slate-500')}>
            <Icon className={cn('w-4 h-4', status === 'running' && 'animate-spin')} />
            {status === 'done' ? 'Done' : status === 'running' ? 'Running' : 'Idle'}
        </span>
    );
}

function ConfigurationFileCard({ configuration, recommended, onUpdate }) {
    const [expanded, setExpanded] = React.useState(false);
    let summary = null;
    try {
        const payload = JSON.parse(configuration.payloadText);
        const components = ['serving', 'encode', 'prefill', 'decode']
            .filter((name) => payload[name])
            .map((name) => {
                const tp = Number(payload[name].tp || payload[name].tensor_parallel_size || 1);
                const replicas = Number(payload[name].replicas || 1);
                return { name, tp, replicas, accelerators: tp * replicas };
            });
        summary = {
            type: payload.type || 'configuration',
            model: payload.model,
            accelerators: components.reduce((total, component) => total + component.accelerators, 0),
            components,
        };
    } catch {
        // YAML/manual content remains editable even when a compact JSON summary is unavailable.
    }
    return (
        <div className={cn('rounded-xl border transition-colors', configuration.selected ? 'border-cyan-500/30 bg-cyan-500/5' : 'border-slate-800 bg-slate-950/30')}>
            <div className="flex flex-wrap items-center gap-2 p-3">
                <input type="checkbox" aria-label={`Select ${configuration.name}`} checked={configuration.selected} onChange={(event) => onUpdate({ selected: event.target.checked })} className="accent-cyan-500" />
                <FileText className="w-4 h-4 text-slate-500" />
                <span className="font-mono text-xs text-slate-200">{configuration.name}</span>
                {recommended && <Badge tone="success" size="xs">Recommended</Badge>}
                <Badge tone="neutral" size="xs">{CANDIDATE_SOURCES.find((source) => source.id === configuration.source)?.label || configuration.source}</Badge>
                {summary && <>
                    <Badge tone="info" size="xs">{summary.type}</Badge>
                    <span className="flex items-center gap-1 text-[10px] text-slate-400"><Cpu className="w-3 h-3" />{summary.accelerators} accelerators</span>
                    {summary.components.map((component) => <span key={component.name} className="font-mono text-[10px] text-slate-500">{component.name[0].toUpperCase()} {component.tp}×{component.replicas}</span>)}
                </>}
                <button type="button" onClick={() => setExpanded((value) => !value)} className="ml-auto flex items-center gap-1 rounded-md border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:border-cyan-500/40 hover:text-cyan-300">
                    <Pencil className="w-3 h-3" /> {expanded ? 'Close editor' : 'Check / Edit'} {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                </button>
            </div>
            {expanded && <div className="border-t border-slate-800 p-3">
                {summary?.model && <p className="mb-2 text-[10px] text-slate-500">Model: <span className="font-mono text-slate-300">{summary.model}</span></p>}
                <textarea value={configuration.payloadText} onChange={(event) => onUpdate({ payloadText: event.target.value })} rows={11} spellCheck={false} className="w-full rounded-lg border border-slate-800 bg-slate-950/70 px-3 py-2 font-mono text-[11px] text-slate-300 outline-none focus:border-cyan-500/40" />
            </div>}
        </div>
    );
}

function GeneratedConfigurationCard({ candidate, selected, onToggle }) {
    const [expanded, setExpanded] = React.useState(false);
    const [view, setView] = React.useState('deployable');
    const { copied, copy } = useClipboard();
    const views = {
        deployable: {
            label: 'Deployable Configuration',
            value: candidate.deployableConfiguration,
        },
        candidate: {
            label: 'CandidateConfig',
            value: candidate.candidateConfig,
        },
        network: {
            label: 'Backend Network',
            value: candidate.deployableConfiguration?.content?.network,
        },
        file: {
            label: 'Saved File',
            value: candidate.configurationFile,
        },
    };
    const content = JSON.stringify(views[view].value || {}, null, 2);
    const copyContent = () => copy(content);
    return (
        <div className={cn('rounded-xl border', selected ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-slate-800 bg-slate-900/40')}>
            <div className="flex flex-wrap items-center gap-3 p-3">
                <input type="checkbox" aria-label={`Deploy ${candidate.name}`} disabled={candidate.deployable === false} checked={selected} onChange={onToggle} className="accent-emerald-500" />
                <Badge tone={candidate.family === 'Predictive' ? 'violet' : candidate.family === 'Existing / User' ? 'success' : 'info'} size="xs">{candidate.sourceLabel}</Badge>
                <span className="font-mono text-sm text-slate-200">{candidate.name}</span>
                <Badge tone="success" size="xs">{candidate.deployableConfiguration?.format || 'rendered'}</Badge>
                <span className="text-xs text-slate-500 flex items-center gap-1"><Cpu className="w-3 h-3" />{candidate.totalGpus} accelerators</span>
                {candidate.encodeTp && <span className="text-xs font-mono text-slate-500">E {candidate.encodeTp}×{candidate.encodeReplicas}</span>}
                {candidate.prefillTp > 0 && <span className="text-xs font-mono text-slate-500">P {candidate.prefillTp}×{candidate.prefillReplicas}</span>}
                <span className="text-xs font-mono text-slate-500">D {candidate.decodeTp}×{candidate.decodeReplicas}</span>
                {candidate.allocatable_kv_cache_gib !== undefined && candidate.allocatable_kv_cache_gib !== null && (
                    <span className="text-xs font-mono text-emerald-400">KV: {candidate.allocatable_kv_cache_gib} GiB{candidate.max_concurrent_requests ? ` · ${candidate.max_concurrent_requests} req` : ''}</span>
                )}
                {candidate.configurationFile && <span className="text-[10px] font-mono text-emerald-400">{candidate.configurationFile.path}</span>}
                <button type="button" onClick={() => setExpanded((value) => !value)} className="ml-auto flex items-center gap-1 rounded-md border border-emerald-500/30 px-2 py-1 text-[10px] text-emerald-300 hover:bg-emerald-500/10">
                    <Eye className="w-3 h-3" /> {expanded ? 'Hide generated content' : 'View generated content'}
                </button>
            </div>
            {expanded && <div className="border-t border-slate-800 p-3">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                    {Object.entries(views).map(([key, item]) => (
                        <button key={key} type="button" onClick={() => setView(key)} className={cn('rounded-md px-2.5 py-1 text-[10px] transition-colors', view === key ? 'bg-emerald-500/15 text-emerald-300' : 'text-slate-500 hover:text-slate-300')}>
                            {item.label}
                        </button>
                    ))}
                    <button type="button" onClick={copyContent} className="ml-auto flex items-center gap-1 rounded-md border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:border-emerald-500/30">
                        {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />} {copied ? 'Copied' : 'Copy'}
                    </button>
                </div>
                <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950/80 p-3 font-mono text-[11px] leading-relaxed text-slate-300">{content}</pre>
            </div>}
        </div>
    );
}

function ProfileCard({ title, badge, children }) {
    return (
        <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
            <div className="mb-3 flex items-center justify-between gap-2">
                <h3 className="text-xs font-bold uppercase tracking-wider text-slate-300">{title}</h3>
                {badge && <Badge tone={badge === 'VERIFIED' ? 'success' : 'info'} size="xs">{badge}</Badge>}
            </div>
            {children}
        </div>
    );
}

function LocalDeploymentRunStatus({ run }) {
    const [displayRun, setDisplayRun] = React.useState(run);
    const [selectedRecordRun, setSelectedRecordRun] = React.useState(null);
    const [selectedCase, setSelectedCase] = React.useState(null);
    const [details, setDetails] = React.useState(null);
    const [loading, setLoading] = React.useState(false);
    const [error, setError] = React.useState('');
    const [actionMessage, setActionMessage] = React.useState('');
    const [searchQuery, setSearchQuery] = React.useState('');
    const [searchResults, setSearchResults] = React.useState([]);
    const [searching, setSearching] = React.useState(false);
    const [hasSearched, setHasSearched] = React.useState(false);
    const [preserveRenderedOverlay, setPreserveRenderedOverlay] = React.useState(false);
    const displayCases = Array.isArray(displayRun?.cases) ? displayRun.cases : [];
    React.useEffect(() => {
        setDisplayRun(run);
        setSelectedRecordRun(null);
        setSelectedCase(null);
        setDetails(null);
        setError('');
        setActionMessage('');
    }, [run?.id]);
    const runStatus = ['succeeded', 'partially_succeeded'].includes(displayRun?.status) ? 'ready' : displayRun?.status === 'failed' ? 'failed' : displayRun?.status === 'cleaned' ? 'neutral' : 'pending';
    // The workspace loads a run once; keep the selected case (and its status
    // chip) fresh while the deployment is still progressing, so a finished
    // deployment stops showing "in progress".
    React.useEffect(() => {
        if (!selectedCase) return undefined;
        const terminal = new Set(['ready', 'failed', 'stopped', 'cleaned', 'cleaned_up', 'cancelled']);
        const selectedRun = selectedCase.runId === run.id ? displayRun : selectedRecordRun;
        const deploymentCase = (Array.isArray(selectedRun?.cases) ? selectedRun.cases : [])
            .find((item) => item.id === selectedCase.caseId);
        if (deploymentCase && terminal.has(deploymentCase.status)) return undefined;
        const timer = window.setInterval(async () => {
            try {
                const refreshed = await refreshLocalDeploymentCase(selectedCase.runId, selectedCase.caseId);
                setDetails(refreshed);
                const updatedRun = await getLocalDeploymentRun(selectedCase.runId);
                if (selectedCase.runId === run.id) setDisplayRun(updatedRun);
                else setSelectedRecordRun(updatedRun);
            } catch {
                // Keep the last good state; the next tick retries.
            }
        }, 5000);
        return () => window.clearInterval(timer);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [selectedCase?.runId, selectedCase?.caseId, displayRun?.status, selectedRecordRun?.status, run?.id]);
    const updateSelectedRun = (updatedRun, { asSelectedRecord = false } = {}) => {
        if (!asSelectedRecord && updatedRun.id === run.id) setDisplayRun(updatedRun);
        else setSelectedRecordRun(updatedRun);
    };
    const selectCase = async (runId, caseId, { asSelectedRecord = false } = {}) => {
        if (selectedCase?.runId === runId && selectedCase.caseId === caseId) {
            setSelectedCase(null);
            setDetails(null);
            setError('');
            return;
        }
        setSelectedCase({ runId, caseId });
        setLoading(true);
        setError('');
        setActionMessage('');
        try {
            const selectedRun = await getLocalDeploymentRun(runId);
            updateSelectedRun(selectedRun, { asSelectedRecord });
            const deploymentCase = (Array.isArray(selectedRun?.cases) ? selectedRun.cases : []).find((item) => item.id === caseId);
            const detail = deploymentCase?.execution_id
                ? await refreshLocalDeploymentCase(runId, caseId)
                : await getLocalDeploymentCase(runId, caseId);
            setDetails(detail);
            updateSelectedRun(await getLocalDeploymentRun(runId), { asSelectedRecord });
        } catch (requestError) {
            setError(requestError instanceof Error ? requestError.message : 'Unable to load deployment details');
        } finally {
            setLoading(false);
        }
    };
    const refresh = async () => {
        if (!selectedCase) return;
        setLoading(true);
        setError('');
        try {
            const refreshed = await refreshLocalDeploymentCase(selectedCase.runId, selectedCase.caseId);
            setDetails(refreshed);
            updateSelectedRun(await getLocalDeploymentRun(selectedCase.runId), {
                asSelectedRecord: selectedCase.runId !== run.id,
            });
        } catch (requestError) {
            setError(requestError instanceof Error ? requestError.message : 'Unable to refresh deployment details');
        } finally {
            setLoading(false);
        }
    };
    const applyAction = async (action, successMessage) => {
        if (!selectedCase) return;
        setLoading(true);
        setError('');
        setActionMessage('');
        try {
            const response = await action(selectedCase.runId, selectedCase.caseId);
            setDetails(response);
            updateSelectedRun(await getLocalDeploymentRun(selectedCase.runId), {
                asSelectedRecord: selectedCase.runId !== run.id,
            });
            setActionMessage(successMessage);
        } catch (requestError) {
            setError(requestError instanceof Error ? requestError.message : 'Unable to update deployment case');
        } finally {
            setLoading(false);
        }
    };
    const searchRecords = async () => {
        const query = searchQuery.trim();
        if (!query.trim()) {
            setSearchResults([]);
            setHasSearched(false);
            return;
        }
        setSearching(true);
        setHasSearched(true);
        setError('');
        try {
            setSearchResults(await searchLocalDeploymentRuns(query));
        } catch (requestError) {
            setSearchResults([]);
            setError(requestError instanceof Error ? requestError.message : 'Unable to search deployment records');
        } finally {
            setSearching(false);
        }
    };
    const loadLatestRecord = async () => {
        setSearching(true);
        setError('');
        try {
            const [latestRun] = await searchLocalDeploymentRuns('');
            if (!latestRun) throw new Error('No persisted deployment records were found');
            setDisplayRun(latestRun);
            setSelectedRecordRun(null);
            setSelectedCase(null);
            setDetails(null);
        } catch (requestError) {
            setError(requestError instanceof Error ? requestError.message : 'Unable to load the latest deployment record');
        } finally {
            setSearching(false);
        }
    };
    const searchForm = (results) => <>
        <div className="mt-1 flex flex-wrap gap-2">
        <form className="flex min-w-0 flex-1 gap-2" onSubmit={(event) => { event.preventDefault(); searchRecords(); }}>
            <Input value={searchQuery} placeholder="Run ID, namespace, guide, or model" onChange={(event) => setSearchQuery(event.target.value)} />
            <Button type="submit" variant="secondary" disabled={!searchQuery.trim()} isLoading={searching}><Search className="h-3.5 w-3.5" />Search</Button>
        </form>
        <Button type="button" variant="secondary" disabled={searching} onClick={loadLatestRecord}><RefreshCw className="h-3.5 w-3.5" />Load latest</Button>
        </div>
        {error && <p className="mt-2 text-xs text-red-300">{error}</p>}
        {hasSearched && !searching && !error && results.length === 0 && <p className="mt-2 text-xs text-slate-500">No deployment records matched this run ID, namespace, guide, or model.</p>}
        {results.length > 0 && <div className="mt-2 max-h-48 space-y-1 overflow-auto">{results.flatMap((record) => (Array.isArray(record.cases) ? record.cases : []).map((deploymentCase) => <button type="button" key={deploymentCase.id} onClick={() => selectCase(record.id, deploymentCase.id, { asSelectedRecord: true })} className="block w-full rounded border border-slate-800 bg-slate-950/60 px-2 py-1.5 text-left text-[10px] text-slate-300 hover:border-slate-700"><span className="block">{record.created_at} · Run {record.id.slice(0, 8)} · {deploymentCase.provider_ref} · {deploymentCase.status}</span><span className="block text-slate-500">{searchedCaseLabel(deploymentCase)}</span></button>))}</div>}
    </>;
    const searchedCaseLabel = (deploymentCase) => {
        try {
            const content = JSON.parse(deploymentCase.create_request.configuration_artifacts[0]?.content || '{}');
            const decode = content.decode || {};
            const replicas = decode.replicaCount || 1;
            const tensorParallelSize = decode.tensorParallelSize || 1;
            return `${content.model?.name || 'Unknown model'} · ${replicas} replica${replicas === 1 ? '' : 's'} · TP ${tensorParallelSize}`;
        } catch {
            return 'Configuration details unavailable';
        }
    };
    if (!run) {
        return <div className="mb-4 rounded-lg border border-slate-800 bg-slate-950/40 p-4"><Label>Search deployment records</Label>{searchForm(searchResults)}{selectedRecordRun && <div className="mt-3 border-t border-slate-800 pt-3"><span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Selected deployment record</span><div className="mt-2 space-y-1">{(Array.isArray(selectedRecordRun.cases) ? selectedRecordRun.cases : []).map((deploymentCase) => <button type="button" key={deploymentCase.id} onClick={() => selectCase(selectedRecordRun.id, deploymentCase.id, { asSelectedRecord: true })} className="block w-full rounded border border-slate-800 bg-slate-950/60 px-2 py-1.5 text-left text-[10px] text-slate-300 hover:border-slate-700"><span className="block">{deploymentCase.provider_ref} · {deploymentCase.status}</span><span className="block text-slate-500">{searchedCaseLabel(deploymentCase)}</span></button>)}</div></div>}</div>;
    }
    return <div className="mb-4 rounded-lg border border-blue-500/20 bg-blue-500/5 p-4">
        <div className="flex flex-wrap items-center gap-2">
            <StatusChip status={runStatus} label={displayRun.status} size="sm" />
            <span className="font-mono text-xs text-slate-200">Run {displayRun.id}</span>
            <span className="text-[10px] text-slate-500">{displayCases.length} configuration{displayCases.length === 1 ? '' : 's'}</span>
        </div>
        <div className="mt-3 space-y-2">
            {displayCases.map((deploymentCase) => {
                const caseStatus = deploymentCase.status === 'ready' ? 'ready' : ['failed', 'cancelled'].includes(deploymentCase.status) ? 'failed' : ['stopped', 'cleaned', 'cleaned_up'].includes(deploymentCase.status) ? 'neutral' : 'pending';
                const content = deploymentCase.create_request.configuration_artifacts[0]?.content ? JSON.parse(deploymentCase.create_request.configuration_artifacts[0].content) : {};
                const decode = content.decode || {};
                const label = `${deploymentCase.provider_ref} + ${decode.replicaCount || 1} replica${decode.replicaCount === 1 ? '' : 's'} + TP ${decode.tensorParallelSize || 1}`;
                return <button type="button" key={deploymentCase.id} onClick={() => selectCase(displayRun.id, deploymentCase.id)} className={cn('flex w-full flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-left text-xs', selectedCase?.caseId === deploymentCase.id ? 'border-blue-500/50 bg-blue-500/10' : 'border-slate-800 bg-slate-950/60 hover:border-slate-700')}>
                    <StatusChip status={caseStatus} label={deploymentCase.status} size="xs" />
                    <span className="font-mono text-slate-300">{label}</span>
                    <span className="text-slate-500">{content.model?.name}</span>
                    {deploymentCase.failure?.detail && <span className="basis-full text-red-300">{deploymentCase.failure.detail}</span>}
                </button>;
            })}
        </div>
        {selectedRecordRun && selectedRecordRun.id !== run.id && <div className="mt-3 border-t border-slate-800 pt-3"><div className="mb-2 flex flex-wrap items-center gap-2"><span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Selected deployment record</span><StatusChip status={['succeeded', 'partially_succeeded'].includes(selectedRecordRun.status) ? 'ready' : selectedRecordRun.status === 'failed' ? 'failed' : selectedRecordRun.status === 'cleaned' ? 'neutral' : 'pending'} label={selectedRecordRun.status} size="xs" /><span className="font-mono text-[10px] text-slate-500">Run {selectedRecordRun.id}</span><button type="button" aria-label="Remove selected deployment record" title="Remove selected deployment record" onClick={() => { setSelectedRecordRun(null); setSelectedCase(null); setDetails(null); setError(''); }} className="ml-auto text-slate-500 hover:text-slate-300"><X className="h-3.5 w-3.5" /></button></div><div className="space-y-1">{selectedRecordRun.cases.map((deploymentCase) => <button type="button" key={deploymentCase.id} onClick={() => selectCase(selectedRecordRun.id, deploymentCase.id, { asSelectedRecord: true })} className={cn('flex w-full flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-left text-xs', selectedCase?.runId === selectedRecordRun.id && selectedCase.caseId === deploymentCase.id ? 'border-blue-500/50 bg-blue-500/10' : 'border-slate-800 bg-slate-950/60 hover:border-slate-700')}><StatusChip status={deploymentCase.status === 'ready' ? 'ready' : ['failed', 'cancelled'].includes(deploymentCase.status) ? 'failed' : ['stopped', 'cleaned', 'cleaned_up'].includes(deploymentCase.status) ? 'neutral' : 'pending'} label={deploymentCase.status} size="xs" /><span className="font-mono text-slate-300">{searchedCaseLabel(deploymentCase)}</span></button>)}</div></div>}
        {selectedCase && <div className="mt-3 rounded-md border border-slate-800 bg-slate-950/60 p-3 text-xs"><div className="mb-3 flex flex-wrap items-center justify-between gap-3"><span className="font-semibold text-slate-300">Kubernetes deployment snapshot</span><div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="secondary" disabled={loading} isLoading={loading} onClick={refresh}><RefreshCw className="h-3.5 w-3.5" />Refresh</Button><Button type="button" size="sm" variant="secondary" disabled={loading || details?.case?.status !== 'ready'} onClick={() => applyAction(stopLocalDeploymentCase, 'Deployment stopped. All replicas are scaled to zero.')}>Stop</Button><Button type="button" size="sm" variant="secondary" disabled={loading || !['stopped', 'failed'].includes(details?.case?.status) && details?.execution?.status !== 'rolled_back'} onClick={() => applyAction(restartLocalDeploymentCase, 'Deployment restart has started.')}>Restart</Button><label className="flex items-center gap-1.5 text-slate-400"><input type="checkbox" checked={preserveRenderedOverlay} onChange={(event) => setPreserveRenderedOverlay(event.target.checked)} className="accent-cyan-500" />Save deployment files</label><Button type="button" size="sm" variant="danger" disabled={loading || ['cleaned', 'cleaned_up'].includes(details?.case?.status)} onClick={async () => { if (await confirmDelete('Delete this deployment and its Kubernetes namespace? This cannot be undone.')) applyAction((runId, caseId) => cleanLocalDeploymentCase(runId, caseId, preserveRenderedOverlay), 'Deployment deleted. Kubernetes resources were cleaned up.'); }}>Delete deployment</Button><Button type="button" size="sm" variant="secondary" aria-label="Close deployment details" title="Close deployment details" onClick={() => { setSelectedCase(null); setDetails(null); setError(''); setActionMessage(''); }}><X className="h-3.5 w-3.5" /></Button></div></div>{error && <p className="mb-3 text-red-300">{error}</p>}{actionMessage && <p className="mb-3 text-emerald-300">{actionMessage}</p>}{loading && !details ? <p className="text-slate-500">Loading deployment details...</p> : details && <CaseDeploymentDetails details={details} />}</div>}
        <div className="mt-4 border-t border-slate-800 pt-3"><Label>Search deployment records</Label>{searchForm(searchResults)}</div>
    </div>;
}

function CaseDeploymentDetails({ details }) {
    const execution = details.execution;
    const artifact = execution?.configuration_artifacts?.[0];
    const snapshot = execution?.diagnostics?.value?.snapshot || {};
    return <div className="space-y-3"><div className="grid grid-cols-1 gap-2 text-slate-400 sm:grid-cols-2"><div><span className="block text-slate-600">Namespace</span><span className="font-mono break-all text-slate-300">{execution?.namespace || 'Not created yet'}</span></div><div><span className="block text-slate-600">Endpoint</span><span className="font-mono break-all text-slate-300">{execution?.endpoint?.url || 'Not ready'}</span></div></div><SnapshotSection title="Configuration" content={artifact?.content || 'Configuration is not available yet.'} /><SnapshotSection title="Pods" content={snapshot.pods || 'No pod snapshot has been captured yet.'} /><SnapshotSection title="Events" content={snapshot.events || 'No event snapshot has been captured yet.'} /><SnapshotSection title="Modelserver logs" content={snapshot.modelserver_logs || 'No modelserver log snapshot has been captured yet.'} /></div>;
}

function SnapshotSection({ title, content }) {
    return <div><span className="mb-1 block text-slate-500">{title}</span><pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded border border-slate-800 bg-slate-950 p-2 font-mono text-[10px] leading-relaxed text-slate-300">{content}</pre></div>;
}

function OfficialGuidePlanning({ ctx }) {
    const {
        workload, setWorkload, st, guideCatalog, guideSelection, setGuideSelection,
        guidePlanningPhase, guidePlanningError, planningResult, handlePlanGuideDeployment, handleUpdatePlannedYaml,
        selectedCandidateIds, setSelectedCandidateIds, remoteTarget, setRemoteTarget,
    } = ctx;
    const [machineMode, setMachineMode] = React.useState('current');
    const [kubernetesMode, setKubernetesMode] = React.useState('auto');
    const [password, setPassword] = React.useState('');
    const [fetchingFingerprint, setFetchingFingerprint] = React.useState(false);
    const [fingerprintError, setFingerprintError] = React.useState('');
    const selectedGuide = guideCatalog?.guides?.find((item) => item.id === guideSelection.guide);
    const accelerators = selectedGuide?.accelerators || [];
    const selectedAccelerator = accelerators.find((item) => item.id === guideSelection.accelerator);
    const modelServers = selectedAccelerator?.modelServers || [];
    const planning = guidePlanningPhase === 'planning';
    const result = planningResult;
    const candidateId = result ? `guide-${guideSelection.guide}-${guideSelection.accelerator}-${guideSelection.modelServer}` : '';
    const remoteReady = remoteTarget.host && remoteTarget.username && remoteTarget.hostFingerprint
        && (remoteTarget.authMethod !== 'key' || remoteTarget.keyPath)
        && (remoteTarget.authMethod !== 'password' || password);

    const [customPatchError, setCustomPatchError] = React.useState('');
    const selectGuide = (guide) => {
        const nextGuide = guideCatalog?.guides?.find((item) => item.id === guide);
        const accelerator = nextGuide?.accelerators?.[0];
        // Custom patches target a specific guide's manifest, so they are dropped when the
        // guide changes; they are otherwise kept when only the accelerator/model server changes.
        setCustomPatchError('');
        setGuideSelection({ guide, accelerator: accelerator?.id || '', modelServer: accelerator?.modelServers?.[0]?.id || '' });
    };
    const selectAccelerator = (accelerator) => {
        const nextAccelerator = accelerators.find((item) => item.id === accelerator);
        setGuideSelection({ ...guideSelection, accelerator, modelServer: nextAccelerator?.modelServers?.[0]?.id || '' });
    };
    const customPatches = guideSelection.customPatches || [];
    const addCustomPatchFiles = async (fileList) => {
        const files = Array.from(fileList || []);
        if (!files.length) return;
        setCustomPatchError('');
        try {
            const added = await Promise.all(files.map(async (file) => ({ path: file.name, content: await file.text() })));
            setGuideSelection({ ...guideSelection, customPatches: [...customPatches, ...added] });
        } catch {
            setCustomPatchError('Could not read one or more selected files.');
        }
    };
    const removeCustomPatch = (index) => {
        setGuideSelection({ ...guideSelection, customPatches: customPatches.filter((_, itemIndex) => itemIndex !== index) });
    };
    const updateRemoteTarget = (field, value) => {
        setFingerprintError('');
        setRemoteTarget({ ...remoteTarget, [field]: value });
    };
    const discoverFingerprint = async () => {
        setFetchingFingerprint(true);
        setFingerprintError('');
        try {
            const response = await fetchHostFingerprint(remoteTarget);
            setRemoteTarget({ ...remoteTarget, hostFingerprint: response.fingerprint });
        } catch (error) {
            setFingerprintError(error instanceof Error ? error.message : 'Could not fetch SSH host fingerprint');
        } finally {
            setFetchingFingerprint(false);
        }
    };
    const generateYaml = () => handlePlanGuideDeployment(machineMode === 'remote' ? {
        mode: 'remote',
        kubernetesMode,
        target: remoteTarget,
        credentials: remoteTarget.authMethod === 'password' ? { password } : {},
    } : { mode: 'current', kubernetesMode });

    return (
        <>
            <p className="mb-5 text-sm text-slate-400">
                Start from official llm-d guide YAML, inspect the selected machine and its Kubernetes environment,
                then generate an editable deployment manifest sized for that environment.
            </p>

            <div className="mb-5 grid grid-cols-1 gap-3 lg:grid-cols-4">
                {[
                    ['1', 'Guide Selection', 'Official YAML'], ['2', 'Environment', 'Current or remote machine'],
                    ['3', 'Model Selection', 'Any model ID'], ['4', 'Generated YAML', result?.status || 'Ready to generate'],
                ].map(([number, title, detail]) => (
                    <div key={number} className={cn('rounded-xl border p-3', result && number === '4' ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-slate-800 bg-slate-950/40')}>
                        <div className="mb-1 flex items-center gap-2"><span className="flex h-5 w-5 items-center justify-center rounded-full bg-cyan-500/15 text-[10px] text-cyan-300">{number}</span><span className="text-xs font-semibold text-slate-200">{title}</span></div>
                        <p className="ml-7 text-[10px] text-slate-500">{detail}</p>
                    </div>
                ))}
            </div>

            <ProfileCard title="Step 1 — Guide Selection" badge="Official llm-d">
                <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                    <div><Label>Guide</Label><Select value={guideSelection.guide} disabled={!guideCatalog} onChange={(event) => selectGuide(event.target.value)}>{guideCatalog?.guides?.map((guide) => <option key={guide.id} value={guide.id}>{guide.label}</option>)}</Select></div>
                    <div><Label>Accelerator</Label><Select value={guideSelection.accelerator} disabled={!accelerators.length} onChange={(event) => selectAccelerator(event.target.value)}>{accelerators.map((accelerator) => <option key={accelerator.id} value={accelerator.id}>{accelerator.id}</option>)}</Select></div>
                    <div><Label>Model server</Label><Select value={guideSelection.modelServer} disabled={!modelServers.length} onChange={(event) => setGuideSelection({ ...guideSelection, modelServer: event.target.value })}>{modelServers.map((server) => <option key={server.id} value={server.id}>{server.id}</option>)}</Select></div>
                </div>
                <p className="mt-3 text-[10px] text-slate-500">Source: {guideCatalog?.repository || 'llm-d/llm-d'}@{guideCatalog?.ref || 'main'} / guides / {guideSelection.guide} / modelserver / {guideSelection.accelerator} / {guideSelection.modelServer}</p>
                <div className="mt-4 border-t border-slate-800 pt-4">
                    <Label>Custom patches (optional)</Label>
                    <p className="mb-2 text-[10px] text-slate-500">
                        For topologies the guide does not ship as-is (e.g. a hand-built RDMA dual-rail overlay), attach one or more
                        small Kubernetes-style YAML patches (matched to a base resource by <code>kind</code> + <code>metadata.name</code>).
                        They apply only to this plan's generated YAML and never change the shared guide catalog or other guides' plans.
                    </p>
                    <label className="flex cursor-pointer items-center justify-center gap-2 border border-dashed border-slate-700 bg-slate-950/40 px-4 py-3 text-xs text-slate-400 hover:border-cyan-500/40 hover:text-cyan-200">
                        <Upload className="h-3.5 w-3.5" />Attach patch YAML file(s)
                        <input type="file" multiple accept=".yaml,.yml,text/yaml,application/yaml" className="hidden" onChange={(event) => { addCustomPatchFiles(event.target.files); event.target.value = ''; }} />
                    </label>
                    {customPatchError && <p className="mt-2 text-xs text-red-300">{customPatchError}</p>}
                    {customPatches.length > 0 && <ul className="mt-2 space-y-1">
                        {customPatches.map((patch, index) => (
                            <li key={`${patch.path}-${index}`} className="flex items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/40 px-2 py-1.5 text-[10px] text-slate-300">
                                <span className="flex items-center gap-1.5 truncate"><FileText className="h-3 w-3 shrink-0 text-slate-500" />{patch.path}</span>
                                <button type="button" onClick={() => removeCustomPatch(index)} className="shrink-0 text-slate-500 hover:text-red-300"><X className="h-3 w-3" /></button>
                            </li>
                        ))}
                    </ul>}
                </div>
            </ProfileCard>

            <div className="my-3 flex justify-center"><ChevronDown className="h-4 w-4 text-slate-600" /></div>
            <ProfileCard title="Step 2 — Deployment Environment" badge={kubernetesMode === 'required' ? 'Kubernetes required' : kubernetesMode === 'disabled' ? 'Planning only' : 'Auto-detect Kubernetes'}>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <button type="button" onClick={() => setMachineMode('current')} className={cn('rounded-lg border p-3 text-left', machineMode === 'current' ? 'border-cyan-500/40 bg-cyan-500/5' : 'border-slate-800')}><div className="mb-1 flex items-center gap-2 text-xs font-semibold text-slate-300"><Cpu className="h-4 w-4 text-cyan-400" />Current machine</div><p className="text-[10px] text-slate-500">Inspect this Prism host. Its active Kubernetes context is discovered automatically when available.</p></button>
                    <button type="button" onClick={() => setMachineMode('remote')} className={cn('rounded-lg border p-3 text-left', machineMode === 'remote' ? 'border-violet-500/40 bg-violet-500/5' : 'border-slate-800')}><div className="mb-1 flex items-center gap-2 text-xs font-semibold text-slate-300"><Network className="h-4 w-4 text-violet-400" />Another machine</div><p className="text-[10px] text-slate-500">Connect over SSH and inspect CPU, RAM, accelerators, and any Kubernetes cluster configured on that machine.</p></button>
                </div>
                <div className="mt-4 rounded-lg border border-slate-800 bg-slate-900/30 p-3">
                    <Label>Kubernetes usage</Label>
                    <Select value={kubernetesMode} onChange={(event) => setKubernetesMode(event.target.value)}>
                        <option value="auto">Auto-detect — use the machine's current Kubernetes context when available</option>
                        <option value="required">Require Kubernetes — stop if cluster preflight cannot pass</option>
                        <option value="disabled">Planning only — inspect machine resources without Kubernetes</option>
                    </Select>
                    <p className="mt-1 text-[10px] text-slate-500">For a remote machine, Prism uses the current kubectl context of the SSH user. No kubeconfig upload is needed.</p>
                </div>
                {machineMode === 'remote' && <div className="mt-4 rounded-lg border border-violet-500/20 bg-violet-500/5 p-3">
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
                        <div className="md:col-span-2"><Label>Host / IP</Label><Input value={remoteTarget.host} placeholder="gpu-node.example.com" onChange={(event) => updateRemoteTarget('host', event.target.value.trim())} /></div>
                        <div><Label>SSH port</Label><Input type="number" min="1" max="65535" value={remoteTarget.port} onChange={(event) => updateRemoteTarget('port', event.target.value)} /></div>
                        <div><Label>Username</Label><Input autoComplete="username" value={remoteTarget.username} onChange={(event) => updateRemoteTarget('username', event.target.value)} /></div>
                    </div>
                    <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
                        <div><Label>Authentication</Label><Select value={remoteTarget.authMethod} onChange={(event) => updateRemoteTarget('authMethod', event.target.value)}><option value="agent">SSH Agent on Prism server</option><option value="key">Private key on Prism server</option><option value="password">One-time password</option></Select></div>
                        {remoteTarget.authMethod === 'key' && <div><Label>Server-side private key path</Label><Input value={remoteTarget.keyPath} placeholder="~/.ssh/id_ed25519" onChange={(event) => updateRemoteTarget('keyPath', event.target.value)} /></div>}
                        {remoteTarget.authMethod === 'password' && <div><Label>SSH password (never saved)</Label><Input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></div>}
                    </div>
                    <div className="mt-3"><Label>SSH host fingerprint</Label><div className="flex gap-2"><Input className="font-mono" value={remoteTarget.hostFingerprint} placeholder="SHA256:…" onChange={(event) => updateRemoteTarget('hostFingerprint', event.target.value.trim())} /><Button type="button" variant="secondary" disabled={fetchingFingerprint || !remoteTarget.host} onClick={discoverFingerprint}>{fetchingFingerprint ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />} Fetch</Button></div></div>
                    <div className="mt-3 flex gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2 text-[10px] text-amber-200/80"><ShieldCheck className="h-4 w-4 shrink-0" />Verify the fingerprint with the machine owner. Credentials are used only for this request.</div>
                    {fingerprintError && <p className="mt-2 text-xs text-red-300">{fingerprintError}</p>}
                </div>}
            </ProfileCard>

            <div className="my-3 flex justify-center"><ChevronDown className="h-4 w-4 text-slate-600" /></div>
            <ProfileCard title="Step 3 — Model Selection">
                <div><Label>Model ID or path</Label><Input value={workload.model} placeholder="Organization/model-name or /path/to/model" onChange={(event) => setWorkload({ ...workload, model: event.target.value })} /><p className="mt-1 text-[10px] text-slate-500">Enter any model identifier; this field is not limited to a preset list.</p></div>
                <div className="mt-4 flex justify-end"><Button disabled={planning || !guideSelection.guide || !guideSelection.accelerator || !guideSelection.modelServer || !workload.model.trim() || (machineMode === 'remote' && !remoteReady)} isLoading={planning} onClick={generateYaml}><WandSparkles className="h-3.5 w-3.5" />Generate deployment YAML</Button></div>
            </ProfileCard>

            {guidePlanningError && <div className="mt-4 rounded-lg border border-amber-500/20 bg-amber-500/5 px-4 py-3 text-xs text-amber-200">{guidePlanningError}</div>}

            {result && <>
                <div className="my-3 flex justify-center"><ChevronDown className="h-4 w-4 text-slate-600" /></div>
                <ProfileCard title="Step 4 — Generated Deployment YAML" badge={result.status}>
                    <div className="mb-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
                        <div><span className="block text-slate-600">Machine</span><span className="text-slate-300">{result.machineProfile?.cpu?.logicalCores} CPU · {result.machineProfile?.memoryGiB} GiB</span></div>
                        <div><span className="block text-slate-600">Accelerator</span><span className="text-slate-300">{result.machineProfile?.accelerator?.count || 0} × {result.machineProfile?.accelerator?.model || 'not detected'}</span></div>
                        <div><span className="block text-slate-600">Guide → planned replicas</span><span className="text-slate-300">{result.summary?.guideReplicas || 'inherited'} → {result.summary?.plannedReplicas || 'inherited'}</span></div>
                        <div><span className="block text-slate-600">Tensor parallelism</span><span className="text-slate-300">TP {result.summary?.tensorParallelSize || 1}</span></div>
                    </div>
                    <div className="mb-3 flex flex-wrap gap-2">{result.source?.files?.map((file) => <Badge key={file} tone="neutral" size="xs">{file.split('/').pop()}</Badge>)}</div>
                    {result.appliedCustomPatches?.length > 0 && <div className="mb-3 rounded-lg border border-cyan-500/20 bg-cyan-500/5 p-2">
                        <p className="mb-1 text-[10px] font-semibold text-cyan-300">Custom patches applied</p>
                        {result.appliedCustomPatches.map((patch, index) => (
                            <p key={`${patch.path}-${index}`} className="text-[10px] text-slate-400">
                                {patch.path}: {patch.action === 'merged' ? 'merged into' : 'added as a new resource,'} {patch.kind}{patch.name ? `/${patch.name}` : ''}
                            </p>
                        ))}
                    </div>}
                    {result.validation?.errors?.map((error) => <p key={error} className="mb-1 text-xs text-red-300">Error: {error}</p>)}
                    {result.validation?.warnings?.map((warning) => <p key={warning} className="mb-1 text-xs text-amber-300">Warning: {warning}</p>)}
                    <div className="mt-3 rounded-lg border border-emerald-500/20 bg-slate-950/70">
                        <div className="flex items-center gap-2 border-b border-slate-800 px-3 py-2"><input type="checkbox" checked={selectedCandidateIds.includes(candidateId)} onChange={(event) => setSelectedCandidateIds(event.target.checked ? [candidateId] : [])} className="accent-emerald-500" /><span className="text-xs text-emerald-300">Use this YAML in the Deployment step</span><Badge tone="neutral" size="xs" className="ml-auto">Editable</Badge></div>
                        <textarea aria-label="Generated deployment YAML" value={result.plannedDeployment?.content || ''} onChange={(event) => handleUpdatePlannedYaml(event.target.value)} rows={24} spellCheck={false} className="w-full resize-y bg-transparent p-3 font-mono text-[10px] leading-relaxed text-slate-300 outline-none" />
                    </div>
                    <p className="mt-2 text-[10px] text-slate-500">The backend resolved model, capacity, replicas, tensor parallelism, and Kubernetes constraints against the official guide. Review or edit this final manifest before deployment.</p>
                </ProfileCard>
            </>}
            {!result && guidePlanningPhase === 'catalog' && <div className="mt-4 flex items-center justify-center gap-2 text-xs text-slate-500"><Loader2 className="h-4 w-4 animate-spin" />Loading official guide catalog…</div>}
            {st('search-candidates') === 'done' && <div className="mt-4 flex items-center gap-2 text-xs text-emerald-400"><CheckCircle2 className="h-4 w-4" />Deployment plan is ready for the Deployment step.</div>}
        </>
    );
}

// ---------------------------------------------------------------------------
// Per-stage body content
// ---------------------------------------------------------------------------
const createDeploymentConfigurationId = (prefix) => `${prefix}-${Date.now()}`;

function StageBody({ stageId, ctx }) {
    const {
        workload, setWorkload, st,
        candidateSourceIds, handleToggleCandidateSource, candidateSearchConfig, setCandidateSearchConfig, candidateSearchError,
        sourceConfigurations, updateSourceConfiguration, configurationPhase,
        candidates, selectedCandidateIds, setSelectedCandidateIds,
        deploymentConfigurations, setDeploymentConfigurations,
        remoteTarget, setRemoteTarget, deploymentEnvironment, setDeploymentEnvironment,
        deployments, activeDeploymentId, setActiveDeploymentId, deploymentError, localDeploymentRun,
        benchmarkRuns, selectedBenchmarkIds, setSelectedBenchmarkIds, tco, rec,
        handleLoadConfigurations, handleGenerateConfigurations, handleDeploy, handleBenchmark, handleAnalyze,
    } = ctx;
    const running = (id) => st(id) === 'running';
    const toggle = (items, value, setter) => setter(items.includes(value) ? items.filter((item) => item !== value) : [...items, value]);
    const activeDeployment = deployments.find((deployment) => deployment.id === activeDeploymentId);
    const deploymentConfiguration = deploymentConfigurations[0];
    const configurationSourceMode = deploymentConfiguration?.sourceMode || 'default-guide';
    const selectedMockConfigurationIds = new Set(deploymentConfigurations
        .filter((configuration) => configuration.sourceMode === 'provided-config')
        .map((configuration) => configuration.id));
    const [uploadError, setUploadError] = React.useState('');
    const [storageVolumes, setStorageVolumes] = React.useState([]);
    React.useEffect(() => {
        const clusterId = sessionStorage.getItem('prism_cluster_server_id');
        if (!clusterId) { setStorageVolumes([]); return undefined; }
        const controller = new AbortController();
        (async () => {
            try {
                const items = await listStorageVolumes({ clusterId, status: 'ready', signal: controller.signal });
                setStorageVolumes(items);
            } catch {
                setStorageVolumes([]);
            }
        })();
        return () => controller.abort();
    }, [stageId]);
    const selectConfigurationSource = (sourceMode) => {
        setUploadError('');
        if (sourceMode === 'provided-config') {
            setDeploymentConfigurations(PROVIDED_SERVICE_CONFIGURATIONS.map((configuration) => ({
                ...configuration,
                sourceMode: 'provided-config',
                sourceFileName: '',
                sourceFilePath: '',
                sourceConfiguration: null,
            })));
            return;
        }
        if (sourceMode === 'default-guide') {
            setDeploymentConfigurations([{
                id: createDeploymentConfigurationId('default-guide'),
                guide: workload.guide || 'optimized-baseline',
                model: workload.model,
                replicas: 1,
                tensorParallelSize: 1,
                sourceMode,
                sourceFileName: '',
                sourceFilePath: '',
                sourceConfiguration: null,
            }]);
            return;
        }
        if (sourceMode === 'suggested-deploy-yaml') {
            setDeploymentConfigurations([{
                id: createDeploymentConfigurationId('suggested-deploy-yaml'),
                guide: workload.guide || 'optimized-baseline',
                model: workload.model,
                replicas: 1,
                tensorParallelSize: 1,
                sourceMode,
                sourceFileName: SUGGESTED_DEPLOY_YAML.filePath.split('/').pop(),
                sourceFilePath: SUGGESTED_DEPLOY_YAML.filePath,
                sourceConfiguration: null,
            }]);
            return;
        }
        setDeploymentConfigurations([{ ...(deploymentConfiguration || { id: createDeploymentConfigurationId('deployment-configuration'), guide: 'optimized-baseline', model: 'Qwen/Qwen3-0.6B', replicas: 1, tensorParallelSize: 1 }), sourceMode, sourceFileName: '', sourceFilePath: '', sourceConfiguration: null }]);
    };
    const toggleMockConfiguration = (selectedId) => {
        const configuration = PROVIDED_SERVICE_CONFIGURATIONS.find((item) => item.id === selectedId);
        if (!configuration) return;
        setDeploymentConfigurations((current) => {
            const selected = current.filter((item) => item.sourceMode === 'provided-config');
            return selectedMockConfigurationIds.has(selectedId)
                ? selected.filter((item) => item.id !== selectedId)
                : [...selected, { ...configuration, sourceMode: 'provided-config', sourceFileName: '', sourceFilePath: '', sourceConfiguration: null }];
        });
    };
    const uploadDeploymentConfiguration = async (event) => {
        const file = event.target.files?.[0];
        event.target.value = '';
        if (!file) return;
        setUploadError('');
        try {
            const content = await file.text();
            setDeploymentConfigurations([{ ...deploymentConfigurationFromFile(file.name, content), id: `uploaded-configuration-${Date.now()}`, sourceFilePath: '', sourceConfiguration: null, uploadedYaml: content }]);
        } catch (error) {
            setUploadError(error instanceof Error ? error.message : 'Unable to read the configuration file');
        }
    };

    switch (stageId) {
        case 'define-workload':
            return (
                <>
                    <p className="text-sm text-slate-400 mb-4">
                        Define the input conditions and objective for this optimization or evaluation task. These values
                        become shared context for candidate generation, deployment sizing, and performance analysis.
                    </p>
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
                        <div>
                            <Label>Model</Label>
                            <Input list="workflow-model-suggestions" value={workload.model} placeholder="Organization/model-name" onChange={(e) => setWorkload({ ...workload, model: e.target.value })} />
                            <datalist id="workflow-model-suggestions">{MOCK_MODELS.map((model) => <option key={model} value={model} />)}</datalist>
                            <p className="text-[10px] text-slate-500 mt-1">Enter any model ID or choose a suggestion.</p>
                        </div>
                        <div>
                            <Label>Guide</Label>
                            <Input value={workload.guide || 'optimized-baseline'} placeholder="optimized-baseline" onChange={(e) => setWorkload({ ...workload, guide: e.target.value.trim() })} />
                            <p className="text-[10px] text-slate-500 mt-1">Used by the default Guide deployment source.</p>
                        </div>
                        <div>
                            <Label>Accelerator / XPU</Label>
                            <Select value={workload.accelerator} onChange={(e) => setWorkload({ ...workload, accelerator: e.target.value })}>
                                {MOCK_ACCELERATORS.map((a) => <option key={a} value={a}>{a}</option>)}
                            </Select>
                            <p className="text-[10px] text-slate-500 mt-1">Intel XPU options are prioritized.</p>
                        </div>
                        <div>
                            <Label>Concurrency</Label>
                            <Input type="number" value={workload.concurrency} onChange={(e) => setWorkload({ ...workload, concurrency: e.target.value })} />
                        </div>
                        <div>
                            <Label>Input length (ISL, tokens)</Label>
                            <Input type="number" value={workload.isl} onChange={(e) => setWorkload({ ...workload, isl: e.target.value })} />
                        </div>
                        <div>
                            <Label>Output length (OSL, tokens)</Label>
                            <Input type="number" value={workload.osl} onChange={(e) => setWorkload({ ...workload, osl: e.target.value })} />
                        </div>
                        <div className="grid grid-cols-2 gap-2">
                            <div>
                                <Label>SLA TTFT (ms)</Label>
                                <Input type="number" value={workload.ttftMs} onChange={(e) => setWorkload({ ...workload, ttftMs: e.target.value })} />
                            </div>
                            <div>
                                <Label>SLA TPOT (ms)</Label>
                                <Input type="number" value={workload.tpotMs} onChange={(e) => setWorkload({ ...workload, tpotMs: e.target.value })} />
                            </div>
                        </div>
                    </div>
                    <div className="mt-4 flex items-center gap-2 text-xs text-emerald-400"><CheckCircle2 className="w-4 h-4" /> Changes are saved automatically and used by downstream tools.</div>
                </>
            );

        case 'search-candidates':
            return <OfficialGuidePlanning ctx={ctx} />;

        case 'legacy-search-candidates':
            return (
                <>
                    <p className="text-sm text-slate-400 mb-4">
                        Select candidate sources, review or edit one or more configuration files, then click Generate.
                        Resolve, capability check, normalization, validation, and render run automatically after Generate.
                    </p>
                    <div className="rounded-xl border border-blue-500/20 bg-blue-500/5 p-4 mb-5">
                        <div className="flex flex-wrap items-start justify-between gap-3 mb-3">
                            <div>
                                <h3 className="text-sm font-semibold text-blue-200">Model & hardware used for configuration</h3>
                                <p className="text-[10px] text-slate-500 mt-0.5">AIC source discovery and the Generate-time capability check use these values. Change them here, then reload the selected sources.</p>
                            </div>
                            <Badge tone="info" size="xs">Capability target</Badge>
                        </div>
                        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                            <div><Label>Model</Label><Input value={workload.model} onChange={(event) => setWorkload({ ...workload, model: event.target.value })} /></div>
                            <div><Label>Hardware profile</Label><Input value={candidateSearchConfig.aicSystemName} placeholder="b60 / h100_sxm" onChange={(event) => setCandidateSearchConfig({ ...candidateSearchConfig, aicSystemName: event.target.value })} /></div>
                            <div><Label>Serving backend</Label><Select value={candidateSearchConfig.aicBackendName} onChange={(event) => setCandidateSearchConfig({ ...candidateSearchConfig, aicBackendName: event.target.value })}><option value="vllm">vLLM</option><option value="sglang">SGLang</option><option value="trtllm">TRT-LLM</option></Select></div>
                            <div><Label>Available accelerators</Label><Input type="number" min="1" max="64" value={candidateSearchConfig.totalGpus} onChange={(event) => setCandidateSearchConfig({ ...candidateSearchConfig, totalGpus: event.target.value })} /></div>
                        </div>
                        <div className="mt-3 flex justify-end">
                            <Button variant="secondary" size="sm" disabled={!candidateSourceIds.length || configurationPhase === 'loading'} isLoading={configurationPhase === 'loading'} onClick={() => handleLoadConfigurations()}>
                                Reload selected source files
                            </Button>
                        </div>
                    </div>
                    <div className="flex items-center gap-2 mb-3 text-xs font-semibold uppercase tracking-wider text-slate-500"><span className="flex h-5 w-5 items-center justify-center rounded-full bg-cyan-500/15 text-cyan-300">1</span>Select candidate source</div>
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-5">
                        {['Predictive', 'Search-based', 'Existing / User'].map((family) => (
                            <div key={family} className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
                                <div className="flex items-center gap-2 mb-3">
                                    {family === 'Predictive' ? <WandSparkles className="w-4 h-4 text-violet-400" /> : family === 'Search-based' ? <Search className="w-4 h-4 text-blue-400" /> : <Database className="w-4 h-4 text-emerald-400" />}
                                    <h3 className="text-xs font-bold uppercase tracking-wider text-slate-300">{family}</h3>
                                </div>
                                <div className="space-y-2">
                                    {CANDIDATE_SOURCES.filter((source) => source.family === family).map((source) => (
                                        <label key={source.id} className={cn('block rounded-lg border px-3 py-2 cursor-pointer transition-colors', candidateSourceIds.includes(source.id) ? 'border-cyan-500/40 bg-cyan-500/10' : 'border-slate-800 hover:border-slate-700')}>
                                            <div className="flex items-center gap-2">
                                                <input type="checkbox" checked={candidateSourceIds.includes(source.id)} onChange={() => handleToggleCandidateSource(source.id)} className="accent-cyan-500" />
                                                <span className="text-xs font-medium text-slate-200">{source.label}</span>
                                            </div>
                                            <p className="text-[10px] text-slate-500 mt-1 ml-5">{source.description}</p>
                                        </label>
                                    ))}
                                </div>
                            </div>
                        ))}
                    </div>
                    <div className="mb-5">
                        <div className="flex items-center justify-between gap-3 mb-3">
                            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-slate-500"><span className="flex h-5 w-5 items-center justify-center rounded-full bg-cyan-500/15 text-cyan-300">2</span>View configuration files · Select / Edit</div>
                            <div className="flex items-center gap-2">
                                {configurationPhase === 'loading' && <span className="flex items-center gap-1.5 text-[10px] text-blue-300"><Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading selected sources…</span>}
                                <Badge tone="info" size="xs">{sourceConfigurations.filter((item) => item.selected).length} selected</Badge>
                            </div>
                        </div>
                        {candidateSourceIds.includes('manual') && sourceConfigurations.some((item) => item.source === 'manual') && (
                            <label className="mb-3 flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed border-emerald-500/30 bg-emerald-500/5 px-4 py-3 text-xs text-emerald-300 hover:border-emerald-400/50">
                                <Upload className="w-4 h-4" /> Upload YAML to replace the manual configuration
                                <input type="file" accept=".yaml,.yml,text/yaml,application/yaml" className="hidden" onChange={async (event) => { const file = event.target.files?.[0]; const manual = sourceConfigurations.find((item) => item.source === 'manual'); if (!file || !manual) return; updateSourceConfiguration(manual.id, { name: file.name, inputMode: 'yaml', payloadText: await file.text(), selected: true }); event.target.value = ''; }} />
                            </label>
                        )}
                        {sourceConfigurations.length > 0 ? (
                            <div className="space-y-2">
                                {sourceConfigurations.map((configuration, index) => (
                                    <ConfigurationFileCard
                                        key={configuration.id}
                                        configuration={configuration}
                                        recommended={sourceConfigurations.findIndex((item) => item.source === configuration.source) === index}
                                        onUpdate={(updates) => updateSourceConfiguration(configuration.id, updates)}
                                    />
                                ))}
                            </div>
                        ) : configurationPhase !== 'loading' && (
                            <div className="rounded-xl border border-dashed border-slate-800 bg-slate-950/30 px-4 py-8 text-center text-xs text-slate-500">
                                Select at least one Candidate Source to view its configuration files.
                            </div>
                        )}
                    </div>
                    {candidateSearchError && (
                        <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 px-4 py-3 mb-4 text-xs text-amber-200">
                            <span className="font-semibold">Source availability:</span> {candidateSearchError} Update Model & hardware above and reload, or choose another source.
                        </div>
                    )}
                    {candidates && (
                        <div className="space-y-2 mb-4 border-t border-slate-800 pt-4">
                            <div className="flex justify-between text-xs text-slate-500"><span className="flex items-center gap-2"><span className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500/15 text-emerald-300">3</span>CandidateConfig(s) → Rendered deployable configurations</span><span>{selectedCandidateIds.length} selected for deployment</span></div>
                            {candidates.map((c) => (
                                <GeneratedConfigurationCard
                                    key={c.id}
                                    candidate={c}
                                    selected={selectedCandidateIds.includes(c.id)}
                                    onToggle={() => toggle(selectedCandidateIds, c.id, setSelectedCandidateIds)}
                                />
                            ))}
                        </div>
                    )}
                    <div className="flex justify-end">
                        <Button variant={st('search-candidates') === 'done' ? 'secondary' : 'primary'} disabled={running('search-candidates') || !sourceConfigurations.some((item) => item.selected)} isLoading={configurationPhase === 'generating'} onClick={handleGenerateConfigurations}>
                            <Code2 className="w-3.5 h-3.5" /> {candidates ? 'Generate again' : 'Generate'}
                        </Button>
                    </div>
                </>
            );

        case 'deployment':
            return (
                <>
                    <p className="text-sm text-slate-400 mb-4">Define one or more service configurations, choose the exact machine and XPU target, then deploy each configuration as a separate service. Switch between deployed services below to inspect status.</p>
                    <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4 mb-4">
                        <h3 className="text-sm font-semibold text-slate-200 mb-3">1. Select deploy node from cluster</h3>
                        <RemoteDeploymentTarget
                            target={remoteTarget}
                            setTarget={setRemoteTarget}
                        />
                        <div className="mt-4 border-t border-slate-800 pt-4">
                            <Label>Cluster software checkout</Label>
                            <div className="mt-1 grid grid-cols-1 gap-3 md:grid-cols-2">
                                <div><Label>llm-d path</Label><Input value={deploymentEnvironment.repository} placeholder="Download llm-d in Cluster settings" readOnly /></div>
                                <div><Label>Version</Label><Input value={deploymentEnvironment.branch} placeholder="Configured by cluster" readOnly /></div>
                            </div>
                        </div>
                    </div>

                    <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4 mb-4">
                        <h3 className="text-sm font-semibold text-slate-200 mb-3">2. Configure model runtime</h3>
                        <div><Label>Model source</Label><div className="mt-1 inline-flex rounded-md border border-slate-800 bg-slate-950/60 p-1"><button type="button" onClick={() => setDeploymentEnvironment({ ...deploymentEnvironment, modelSource: 'download', mountPath: '', mountModelName: '', storageVolumeId: '' })} className={cn('rounded px-3 py-1.5 text-xs', deploymentEnvironment.modelSource === 'download' ? 'bg-blue-500/20 text-blue-100' : 'text-slate-400 hover:text-slate-200')}>Download model</button><button type="button" onClick={() => setDeploymentEnvironment({ ...deploymentEnvironment, modelSource: 'storage-volume', mountPath: '', mountModelName: '' })} className={cn('rounded px-3 py-1.5 text-xs', deploymentEnvironment.modelSource === 'storage-volume' ? 'bg-blue-500/20 text-blue-100' : 'text-slate-400 hover:text-slate-200')}>Use registered storage volume</button><button type="button" onClick={() => setDeploymentEnvironment({ ...deploymentEnvironment, modelSource: 'mount', storageVolumeId: '' })} className={cn('rounded px-3 py-1.5 text-xs', deploymentEnvironment.modelSource === 'mount' ? 'bg-blue-500/20 text-blue-100' : 'text-slate-400 hover:text-slate-200')}>Ad-hoc host path (not recommended)</button></div></div>
                        {deploymentEnvironment.modelSource === 'storage-volume' && (
                            <div className="mt-3">
                                <Label>Storage volume</Label>
                                {storageVolumes.length ? (
                                    <Select
                                        value={deploymentEnvironment.storageVolumeId}
                                        onChange={(event) => setDeploymentEnvironment({ ...deploymentEnvironment, storageVolumeId: event.target.value })}
                                    >
                                        <option value="">Select a storage volume</option>
                                        {storageVolumes.map((volume) => (
                                            <option key={volume.id} value={volume.id}>{volume.name} ({volume.kind})</option>
                                        ))}
                                    </Select>
                                ) : (
                                    <p className="mt-1 text-xs text-slate-500">
                                        No ready storage volumes for this cluster. Register one on the Storage page first.
                                    </p>
                                )}
                            </div>
                        )}
                        {deploymentEnvironment.modelSource === 'mount' && <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2"><div><Label>Model cache host path</Label><Input value={deploymentEnvironment.mountPath} placeholder="/data/models" onChange={(event) => setDeploymentEnvironment({ ...deploymentEnvironment, mountPath: event.target.value.trim() })} /></div><div><Label>Mounted model name</Label><Input value={deploymentEnvironment.mountModelName} placeholder="Qwen3-0.6B" onChange={(event) => setDeploymentEnvironment({ ...deploymentEnvironment, mountModelName: event.target.value.trim() })} /></div></div>}
                        <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-3">
                            <div><Label>HTTP_PROXY</Label><Input value={deploymentEnvironment.httpProxy} onChange={(event) => setDeploymentEnvironment({ ...deploymentEnvironment, httpProxy: event.target.value.trim() })} /></div>
                            <div><Label>HTTPS_PROXY</Label><Input value={deploymentEnvironment.httpsProxy} onChange={(event) => setDeploymentEnvironment({ ...deploymentEnvironment, httpsProxy: event.target.value.trim() })} /></div>
                            <div><Label>NO_PROXY</Label><Input value={deploymentEnvironment.noProxy} onChange={(event) => setDeploymentEnvironment({ ...deploymentEnvironment, noProxy: event.target.value.trim() })} /></div>
                        </div>
                        <div className="mt-3"><Label>Model server image</Label><div className="mt-1 inline-flex rounded-md border border-slate-800 bg-slate-950/60 p-1"><button type="button" onClick={() => setDeploymentEnvironment({ ...deploymentEnvironment, imageMode: 'use-upstream-image' })} className={cn('rounded px-3 py-1.5 text-xs', deploymentEnvironment.imageMode === 'use-upstream-image' ? 'bg-blue-500/20 text-blue-100' : 'text-slate-400 hover:text-slate-200')}>Use upstream image</button><button type="button" onClick={() => setDeploymentEnvironment({ ...deploymentEnvironment, imageMode: 'build-from-source' })} className={cn('rounded px-3 py-1.5 text-xs', deploymentEnvironment.imageMode === 'build-from-source' ? 'bg-blue-500/20 text-blue-100' : 'text-slate-400 hover:text-slate-200')}>Build image from source</button></div></div>
                        {deploymentEnvironment.imageMode === 'build-from-source' ? <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2"><div><Label>Source URL</Label><Input value={deploymentEnvironment.buildSourceUrl} placeholder="https://github.com/org/project.git" onChange={(event) => setDeploymentEnvironment({ ...deploymentEnvironment, buildSourceUrl: event.target.value.trim() })} /></div><div><Label>Target image name</Label><Input value={deploymentEnvironment.targetImageName} placeholder="registry.example.com/team/vllm:dev" onChange={(event) => setDeploymentEnvironment({ ...deploymentEnvironment, targetImageName: event.target.value.trim() })} /></div></div> : <div className="mt-3"><Label>Upstream image</Label><Input value={deploymentEnvironment.upstreamImage} placeholder="ghcr.io/llm-d/llm-d-xpu" onChange={(event) => setDeploymentEnvironment({ ...deploymentEnvironment, upstreamImage: event.target.value.trim() })} /></div>}
                    </div>

                    <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4 mb-4">
                        <div className="mb-3"><h3 className="text-sm font-semibold text-slate-200">3. Configure services</h3><p className="mt-0.5 text-[10px] text-slate-500">Choose the source for one deployable service configuration.</p></div>
                        <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
                            {[
                                ['default-guide', 'Use default guide', 'Use the Guide from Define Workload'],
                                ['suggested-deploy-yaml', 'Use suggested deploy YAML', 'Suggested optimized-baseline patch-vllm.yaml'],
                                ['provided-config', 'Use generated config', 'Mock Configuration API results'],
                                ['upload-file', 'Upload YAML', 'Replace a Guide YAML with a local file'],
                                ['historical-file', 'Select from historical file', 'Database-backed history'],
                            ].map(([mode, label, description]) => <button key={mode} type="button" disabled={mode === 'historical-file'} onClick={() => selectConfigurationSource(mode)} className={cn('border p-3 text-left transition-colors', configurationSourceMode === mode ? 'border-cyan-500/50 bg-cyan-500/10' : 'border-slate-800 bg-slate-900/30 hover:border-slate-700', mode === 'historical-file' && 'cursor-not-allowed opacity-50')}>
                                <span className="block text-xs font-semibold text-slate-200">{label}</span><span className="mt-1 block text-[10px] text-slate-500">{description}</span>
                            </button>)}
                        </div>
                        {configurationSourceMode === 'default-guide' && <div className="mt-3 border border-slate-800 bg-slate-900/30 px-3 py-2 text-xs text-slate-400"><span className="font-mono text-slate-200">{deploymentConfiguration?.guide}</span><span className="ml-2 text-slate-500">{deploymentConfiguration?.model} · 1 replica · TP 1</span></div>}
                        {configurationSourceMode === 'suggested-deploy-yaml' && <div className="mt-3 flex items-center gap-3 border border-cyan-500/40 bg-cyan-500/5 px-3 py-2 text-xs text-slate-200"><FileText className="h-4 w-4 text-cyan-300" /><span className="font-mono">{SUGGESTED_DEPLOY_YAML.filePath}</span></div>}
                        {configurationSourceMode === 'provided-config' && <div className="mt-3 space-y-2">{PROVIDED_SERVICE_CONFIGURATIONS.map((configuration) => <label key={configuration.id} className={cn('flex cursor-pointer items-center gap-3 border px-3 py-2 text-xs', selectedMockConfigurationIds.has(configuration.id) ? 'border-cyan-500/40 bg-cyan-500/5 text-slate-200' : 'border-slate-800 bg-slate-900/30 text-slate-400')}><input type="checkbox" checked={selectedMockConfigurationIds.has(configuration.id)} onChange={() => toggleMockConfiguration(configuration.id)} className="accent-cyan-500" /><span className="font-mono">{configuration.id}</span><span className="ml-auto text-[10px] text-slate-500">{configuration.model} · {configuration.replicas} replica · TP {configuration.tensorParallelSize}</span></label>)}</div>}
                        {configurationSourceMode === 'upload-file' && <div className="mt-3"><label className="flex cursor-pointer items-center justify-center gap-2 border border-dashed border-cyan-500/40 bg-cyan-500/5 px-4 py-4 text-xs text-cyan-200 hover:border-cyan-400"><Upload className="h-4 w-4" />{deploymentConfiguration?.sourceFileName ? `Replace ${deploymentConfiguration.sourceFileName}` : 'Upload local YAML or JSON'}<input type="file" accept=".yaml,.yml,.json,text/yaml,application/yaml,application/json" className="hidden" onChange={uploadDeploymentConfiguration} /></label>{uploadError && <p role="alert" className="mt-2 text-xs text-red-300">{uploadError}</p>}</div>}
                        <div className="mt-4 flex justify-end"><Button type="button" disabled={running('deployment')} isLoading={running('deployment')} onClick={handleDeploy}><Rocket className="h-3.5 w-3.5" />Start deploy</Button></div>
                    </div>

                    {deploymentError && <div role="alert" className="mb-4 rounded-lg border border-red-500/25 bg-red-500/5 px-4 py-3 text-xs text-red-300">{deploymentError}</div>}

                    <div className="border-t border-slate-800 pt-4"><div className="flex items-center gap-2 mb-3 text-sm font-semibold text-slate-300"><History className="w-4 h-4" /> Deployment status</div><LocalDeploymentRunStatus run={localDeploymentRun} />{deployments.length ? <><div className="flex gap-2 overflow-x-auto pb-2 mb-3">{deployments.map((deployment) => <button key={deployment.id} onClick={() => setActiveDeploymentId(deployment.id)} className={cn('shrink-0 rounded-lg border px-3 py-2 text-left transition-colors', deployment.id === activeDeploymentId ? 'border-emerald-500/40 bg-emerald-500/10' : 'border-slate-800 bg-slate-900/30 hover:border-slate-700')}><div className="flex items-center gap-2"><span className="font-mono text-xs text-slate-200">{deployment.name}</span><StatusChip status={deployment.status === 'ready' ? 'ready' : deployment.status === 'failed' ? 'failed' : 'pending'} label={deployment.status} size="xs" /></div><p className="text-[10px] text-slate-500 mt-1">{deployment.candidateName}</p></button>)}</div>{activeDeployment && <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4 space-y-3"><div className="flex flex-wrap items-center gap-2"><StatusChip status={activeDeployment.status === 'ready' ? 'ready' : activeDeployment.status === 'failed' ? 'failed' : 'pending'} label={activeDeployment.status} size="sm" /><span className="font-mono text-sm text-slate-200">{activeDeployment.name}</span><span className="text-xs text-slate-500">{activeDeployment.candidateName}</span><span className="ml-auto text-[10px] text-slate-600">{new Date(activeDeployment.createdAt).toLocaleString()}</span></div><div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs"><div><span className="block text-slate-600">Cluster</span><span className="text-slate-300">{activeDeployment.cluster}</span></div><div><span className="block text-slate-600">Machine</span><span className="text-slate-300">{MOCK_MACHINE_TYPES.find((machine) => machine.id === activeDeployment.machineType)?.label || activeDeployment.machineType}</span></div><div><span className="block text-slate-600">Accelerator</span><span className="text-slate-300">{activeDeployment.accelerator}</span></div><div><span className="block text-slate-600">Allocation</span><span className="text-slate-300">{activeDeployment.nodes} × {activeDeployment.gpusPerNode} cards</span></div></div><p className="font-mono text-xs text-slate-500 break-all">{activeDeployment.endpoint || 'Gateway endpoint not available yet'}</p><div className="flex flex-wrap gap-1">{(activeDeployment.pods || []).map((pod) => <Badge key={pod.name} tone={pod.ready ? 'success' : 'warning'} size="xs">{pod.name} · {pod.status}{pod.reason ? ` · ${pod.reason}` : ''}</Badge>)}</div>{activeDeployment.error && <p className="text-xs text-red-300">{activeDeployment.error}</p>}</div>}</> : <p className="text-xs text-slate-600">No deployments yet.</p>}</div>
                </>
            );

        case 'benchmark':
            return (
                <>
                    <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 px-4 py-3 mb-4">
                        <div className="flex items-center gap-2 text-sm font-semibold text-blue-200"><FlaskConical className="w-4 h-4" /> Independent Benchmark tool</div>
                        <p className="text-xs text-slate-400 mt-1">Benchmark measures a deployed service and produces the data required by Performance & TCO. It does not depend on Simulation.</p>
                    </div>
                    <div className="mb-4"><Label>Deployed service</Label><Select value={activeDeploymentId} onChange={(e) => setActiveDeploymentId(e.target.value)}><option value="">Select a ready service…</option>{deployments.map((deployment) => <option key={deployment.id} value={deployment.id}>{deployment.name} · {deployment.cluster} · {deployment.status}</option>)}</Select></div>
                    <div className="flex justify-end mb-5">
                        <Button disabled={!activeDeploymentId || running('benchmark')} isLoading={running('benchmark')} onClick={handleBenchmark}><FlaskConical className="w-3.5 h-3.5" /> Run benchmark</Button>
                    </div>
                    <h4 className="text-xs font-bold uppercase tracking-wider text-slate-500 mb-2">Benchmark runs</h4>
                    <div className="space-y-2">{benchmarkRuns.map((run) => <div key={run.id} className="rounded-lg border border-slate-800 p-3 text-xs"><div className="flex justify-between"><span className="font-mono text-slate-200">{run.name}</span><span className="text-slate-600">{new Date(run.createdAt).toLocaleTimeString()}</span></div><div className="flex gap-3 mt-2 text-slate-400"><span>{run.measured.throughputTps} tok/s</span><span>TTFT {run.measured.ttftMs}ms</span><span>TPOT {run.measured.tpotMs}ms</span></div></div>)}{!benchmarkRuns.length && <p className="text-xs text-slate-600">No benchmark runs yet.</p>}</div>
                </>
            );

        case 'performance-tco':
            return (
                <>
                    <p className="text-sm text-slate-400 mb-4">Select one or more benchmark runs to compare measured benchmark data, performance, and TCO. Simulation data is intentionally not required.</p>
                    <div className="space-y-2 mb-4">{benchmarkRuns.map((run) => <label key={run.id} className={cn('flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2 cursor-pointer', selectedBenchmarkIds.includes(run.id) ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-slate-800')}><input type="checkbox" className="accent-emerald-500" checked={selectedBenchmarkIds.includes(run.id)} onChange={() => toggle(selectedBenchmarkIds, run.id, setSelectedBenchmarkIds)} /><span className="font-mono text-xs text-slate-200">{run.name}</span><span className="text-xs text-slate-500">{run.deploymentName}</span><span className="ml-auto text-xs text-slate-400">{run.measured.throughputTps} tok/s · {run.totalGpus} GPU</span></label>)}</div>
                    {!benchmarkRuns.length && <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4 mb-4 text-xs text-amber-300">Performance analysis requires benchmark data. Run a benchmark in the previous step first.</div>}
                    <div className="flex justify-end mb-5"><Button disabled={!selectedBenchmarkIds.length || running('performance-tco')} isLoading={running('performance-tco')} onClick={handleAnalyze}><DollarSign className="w-3.5 h-3.5" /> Analyze performance & TCO</Button></div>
                    {tco && <><div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4 mb-4"><div className="flex items-center justify-between mb-3"><h3 className="text-sm font-semibold text-slate-200">Performance / TCO Pareto</h3><Badge tone="success" size="xs">lower cost · higher throughput</Badge></div><div className="relative h-48 border-l border-b border-slate-700 ml-8 mb-6"><span className="absolute -left-9 top-1/2 -rotate-90 text-[10px] text-slate-600">Throughput</span><span className="absolute left-1/2 -bottom-5 text-[10px] text-slate-600">Cost / 1M tokens →</span>{tco.map((row, index) => { const costs = tco.map((item) => item.costPerMillionTokens); const throughputs = tco.map((item) => item.throughputTps); const x = costs.length === 1 ? 50 : 10 + ((row.costPerMillionTokens - Math.min(...costs)) / (Math.max(...costs) - Math.min(...costs) || 1)) * 80; const y = throughputs.length === 1 ? 50 : 10 + ((row.throughputTps - Math.min(...throughputs)) / (Math.max(...throughputs) - Math.min(...throughputs) || 1)) * 80; const pareto = !tco.some((other) => other.costPerMillionTokens <= row.costPerMillionTokens && other.throughputTps >= row.throughputTps && other.candidateId !== row.candidateId); return <div key={row.benchmarkId} title={`${row.name}: $${row.costPerMillionTokens}/1M, ${row.throughputTps} tok/s`} className={cn('absolute -translate-x-1/2 translate-y-1/2 w-4 h-4 rounded-full border-2', pareto ? 'bg-emerald-400 border-emerald-200 shadow-[0_0_12px_rgba(52,211,153,.7)]' : 'bg-slate-500 border-slate-300')} style={{ left: `${x}%`, bottom: `${y}%` }}><span className="absolute left-5 -top-1 text-[10px] text-slate-400 whitespace-nowrap">{index + 1}</span></div>; })}</div></div><div className="overflow-x-auto mb-4"><table className="w-full text-xs"><thead><tr className="text-left text-slate-500 border-b border-slate-800"><th className="py-2">Config</th><th>Benchmark</th><th>TTFT</th><th>TPOT</th><th>Throughput</th><th>$/month</th><th>$/1M tok</th></tr></thead><tbody>{tco.map((row) => <tr key={row.benchmarkId} className="border-b border-slate-900 font-mono text-slate-300"><td className="py-2">{row.name}</td><td>{row.benchmarkId.slice(-8)}</td><td>{row.ttftMs}ms</td><td>{row.tpotMs}ms</td><td>{row.throughputTps}</td><td>${row.monthlyCost.toLocaleString()}</td><td className="text-emerald-400">${row.costPerMillionTokens}</td></tr>)}</tbody></table></div></>}
                    {rec && <div className="rounded-xl border border-violet-500/30 bg-violet-500/5 p-4"><div className="flex items-center gap-2 mb-2"><Sparkles className="w-4 h-4 text-violet-400" /><span className="text-sm font-semibold text-violet-200">Agent recommendation</span><StatusChip status={rec.slaMet ? 'approved' : 'warnings'} label={rec.slaMet ? 'Meets SLA' : 'SLA at risk'} size="xs" /></div><p className="text-xs text-slate-400 leading-relaxed">{rec.explanation}</p></div>}
                </>
            );

        default:
            return null;
    }
}

// ---------------------------------------------------------------------------
// A single stage page (one tab). The left app nav is the pipeline; this page
// renders one stage plus "previous / next" guidance to walk the flow.
// ---------------------------------------------------------------------------
export default function OptimizationStage({ stageView, onNavigateBack, onNavigate, onToggleMobileNav }) {
    const ctx = useWorkflow();
    const stage = stageByView(stageView) || STAGES[0];
    const idx = STAGES.findIndex((s) => s.id === stage.id);
    const prev = stage.id === 'benchmark'
        ? stageByView('opt-deploy')
        : stage.id === 'performance-tco'
            ? stageByView('opt-benchmark')
            : STAGES[idx - 1];
    const next = stage.id === 'benchmark'
        ? stageByView('opt-performance')
        : STAGES[idx + 1];
    const status = ctx.st(stage.id);

    return (
        <div className="min-h-screen pt-16">
            <WellLitHeader
                pageTitle={stage.label}
                badgeLabel="Optimization"
                onNavigateBack={onNavigateBack}
                onToggleMobileNav={onToggleMobileNav}
                isPrototype
            />

            <div className="max-w-4xl mx-auto px-4 py-8 space-y-5 sm:px-6">
                {/* Context strip: position in the pipeline + progress + mock tag */}
                <div className={cn(
                    'flex flex-wrap items-center justify-between gap-3',
                )}>
                    <div className="flex items-center gap-2 text-xs text-slate-400">
                        <button onClick={() => onNavigate?.('optimization-workspace')} className="hover:text-slate-200 cursor-pointer">Optimization</button>
                        <ChevronRight className="w-3 h-3 text-slate-600" />
                        <span className="font-mono">{stage.parallel ? 'Independent tool' : `Step ${stage.step}`}</span>
                        <span className="text-slate-600">·</span>
                        <span>{stage.group}</span>
                        {stage.parallel && <Badge tone="violet" size="xs" className="font-mono">parallel branch</Badge>}
                        {stage.agent && <Badge tone="info" size="xs" className="font-mono ml-1">agent?</Badge>}
                        <MockChip stepId={stage.id} />
                    </div>
                    <div className="flex items-center gap-3">
                        <div className="w-32 h-1.5 rounded-full bg-slate-800 overflow-hidden">
                            <div className="h-full bg-gradient-to-r from-emerald-500 to-teal-400 transition-all" style={{ width: `${ctx.progressPct}%` }} />
                        </div>
                        <span className="text-[11px] font-mono text-slate-500">{ctx.progressPct}%</span>
                    </div>
                </div>

                {/* Stage content */}
                <Panel padding="none" className="overflow-hidden">
                    <div className="flex items-center justify-between gap-4 px-5 py-3.5 border-b border-theme-border">
                        <h2 className="text-base font-bold text-theme-text truncate">{stage.label}</h2>
                        <StatusText status={status} />
                    </div>
                    <div className="p-5">
                        <StageBody stageId={stage.id} ctx={ctx} />
                    </div>
                </Panel>

                {/* Guidance: previous / next */}
                <div className={cn(
                    'rounded-2xl border bg-slate-900/40 px-4 py-3 flex flex-wrap items-center justify-between gap-3',
                    stage.parallel ? 'border-violet-500/20' : 'border-slate-800',
                )}>
                    {prev ? (
                        <Button variant="secondary" onClick={() => onNavigate?.(prev.view)}>
                            <ArrowLeft className="w-3.5 h-3.5" /> {prev.label}
                        </Button>
                    ) : <span />}
                    <span className="text-xs text-slate-500">
                        {stage.id === 'deployment' ? 'Choose either independent evaluation branch'
                            : stage.id === 'benchmark' ? 'Independent branch · output can feed Performance & TCO'
                                : next ? 'Next step in the flow' : 'Final step'}
                    </span>
                    {stage.id === 'deployment' ? (
                        <div className="flex gap-2">
                            <Button onClick={() => onNavigate?.('opt-benchmark')}><FlaskConical className="w-3.5 h-3.5" /> Benchmark</Button>
                        </div>
                    ) : next ? (
                        <Button onClick={() => onNavigate?.(next.view)}>
                            {stage.parallel ? 'Analyze benchmark: ' : 'Continue: '}{next.label} <ArrowRight className="w-3.5 h-3.5" />
                        </Button>
                    ) : (
                        <Button onClick={() => onNavigate?.('optimization-evaluate')}>
                            Finish · View evaluations <ChevronRight className="w-3.5 h-3.5" />
                        </Button>
                    )}
                </div>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Overview / landing: a map of the whole pipeline as cards (no left rail — the
// app navigation already lists the stages). Lets the user start or resume.
// ---------------------------------------------------------------------------
export function OptimizationOverview({ onNavigateBack, onNavigate, onToggleMobileNav }) {
    const ctx = useWorkflow();
    const firstIncomplete = STAGES.find((s) => ctx.st(s.id) !== 'done') || STAGES[0];

    return (
        <div className="min-h-screen pt-16">
            <WellLitHeader
                pageTitle="Optimization"
                badgeLabel="Optimization"
                onNavigateBack={onNavigateBack}
                onToggleMobileNav={onToggleMobileNav}
                isPrototype
            />
            <div className="max-w-4xl mx-auto px-4 sm:px-6 py-8 space-y-6">
                <div className="rounded-2xl border border-amber-500/30 bg-amber-500/5 px-5 py-4 flex items-start gap-3">
                    <div className="text-sm text-slate-300 space-y-1">
                        <p className="font-semibold text-amber-300">Prototype — backend is mocked</p>
                        <p className="text-slate-400">
                            Define → source candidates → deploy → Benchmark → analyze Benchmark data. Simulation is available from Services / Simulation.
                            Missing backends are stubbed in <code className="text-slate-300 font-mono text-xs">mockBackend.js</code>;
                            hover a stage tag to see the real backend it stands in for.
                        </p>
                    </div>
                </div>

                <div className="flex items-center gap-3">
                    <div className="flex-1 h-2 rounded-full bg-slate-800 overflow-hidden border border-slate-900">
                        <div className="h-full bg-gradient-to-r from-emerald-500 to-teal-400 transition-all duration-500" style={{ width: `${ctx.progressPct}%` }} />
                    </div>
                    <span className="text-xs font-mono text-slate-400 shrink-0">{ctx.progressPct}%</span>
                    <Button onClick={() => onNavigate?.(firstIncomplete.view)}>
                        {ctx.progressPct === 0 ? 'Start' : ctx.progressPct === 100 ? 'Review' : 'Resume'} <ArrowRight className="w-3.5 h-3.5" />
                    </Button>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {STAGES.map((s) => {
                        const Icon = STAGE_ICON[s.id] || Circle;
                        const status = ctx.st(s.id);
                        return (
                            <button
                                key={s.id}
                                onClick={() => onNavigate?.(s.view)}
                                className="text-left rounded-2xl border border-slate-800 bg-slate-900/40 hover:border-slate-600 p-4 flex items-start gap-3 transition-colors cursor-pointer"
                            >
                                <div className={cn('w-9 h-9 rounded-xl flex items-center justify-center shrink-0 border',
                                    status === 'done' ? 'bg-emerald-500/15 border-emerald-500/40 text-emerald-400'
                                        : status === 'running' ? 'bg-slate-800 border-blue-500/50 text-blue-300'
                                            : 'bg-slate-900 border-slate-700 text-slate-400')}>
                                    {status === 'done' ? <CheckCircle2 className="w-4.5 h-4.5" />
                                        : status === 'running' ? <Loader2 className="w-4 h-4 animate-spin" />
                                            : <Icon className="w-4.5 h-4.5" />}
                                </div>
                                <div className="min-w-0 flex-1">
                                    <div className="flex items-center gap-2">
                                        <span className="text-[11px] font-mono text-slate-500">{s.parallel ? 'Independent tool' : `Step ${s.step}`}</span>
                                        {s.parallel && <Badge tone="violet" size="xs" className="font-mono">parallel</Badge>}
                                        {s.agent && <Badge tone="info" size="xs" className="font-mono">agent?</Badge>}
                                        <MockChip stepId={s.id} />
                                    </div>
                                    <h3 className="text-sm font-semibold text-theme-text truncate">{s.label}</h3>
                                    <span className="text-[10px] uppercase tracking-wider text-slate-600 font-bold">{s.group}</span>
                                </div>
                            </button>
                        );
                    })}
                </div>
            </div>
        </div>
    );
}
