// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import { useMonitoringOperation } from './useMonitoringOperation';
import { openMonitoringLink } from './monitoringLinks';
import { requestMonitoringJson } from '../../api/monitoringClient';
import React, { useEffect, useState } from 'react';
import {
    Activity,
    AlertTriangle,
    AlertOctagon,
    BarChart3,
    Boxes,
    CheckCircle2,
    CloudCog,
    Cpu,
    RefreshCw,
    Rocket,
    ShieldCheck,
    Sparkles,
    Upload,
    Webcam,
    X,
} from 'lucide-react';
import { Badge, Button, EmptyState, Input, LoadingState, SectionLabel, Select, ModuleHeader, ModulePage } from '../ui';
import { cn } from '../../utils/cn';
import { AcceleratorObservabilityPanel } from './AcceleratorObservabilityPanel';
import { DeploymentObservabilityPanel } from './DeploymentObservabilityPanel';
import { DeploymentProfilingPanel } from './DeploymentProfilingPanel';
import { ClusterStackComponentTable } from './ClusterStackComponentTable';
import { ClusterStackOperationProgress } from './ClusterStackOperationProgress';
import { ClusterStackSummary } from './ClusterStackSummary';
import { InstallClusterStackModal } from './InstallClusterStackModal';
import { getClusterStackLinks, getClusterStackOperation } from './clusterMonitoringStackBackend';
import { useClusterMonitoringStackStatus } from './useClusterMonitoringStackStatus';

const DEFAULT_NAMESPACE = 'llm-d-monitoring';

const SERVICE_TABS = [
    { value: 'cluster', label: 'Cluster', icon: CloudCog },
    { value: 'accelerator', label: 'Accelerators', icon: Cpu },
    { value: 'deployment', label: 'Deployments', icon: Rocket },
];

export default function ClusterMonitoringStackDashboard({ onToggleMobileNav, initialClusterId, initialProfileDeployment }) {
    const [namespaceInput, setNamespaceInput] = useState(DEFAULT_NAMESPACE);
    const [namespace, setNamespace] = useState(DEFAULT_NAMESPACE);
    const [clusters, setClusters] = useState([]);
    const [clusterId, setClusterId] = useState('');
    const [clustersError, setClustersError] = useState(null);
    const [installOpen, setInstallOpen] = useState(false);
    const [reinstallMode, setReinstallMode] = useState(false);
    const [activeServiceTab, setActiveServiceTab] = useState('cluster');
    const [profilingDeployment, setProfilingDeployment] = useState(null);
    const [operation, setOperation] = useState(null);
    const [operationError, setOperationError] = useState(null);
    const [installSuccess, setInstallSuccess] = useState(null);
    const [completedOperationId, setCompletedOperationId] = useState(null);
    const [openingLink, setOpeningLink] = useState(null);
    const [linksError, setLinksError] = useState(null);
    const status = useClusterMonitoringStackStatus(namespace, clusterId);
    const refreshStatus = status.refresh;
    const snapshot = status.data;
    const snapshotOperationId = snapshot?.active_operation_id;
    const operationId = operation?.operation_id
        || (snapshotOperationId !== completedOperationId ? snapshotOperationId : null);

    useEffect(() => {
        if (initialProfileDeployment) {
            setActiveServiceTab('deployment');
            setProfilingDeployment(initialProfileDeployment);
        }
    }, [initialProfileDeployment]);

    useMonitoringOperation({
        operationId, operationStatus: operation?.status, completedOperationId, namespace,
        loadOperation: getClusterStackOperation, refreshStatus, setOperation, setOperationError,
        setCompletedOperationId, setInstallSuccess,
    });

    useEffect(() => {
        if (!installSuccess) return undefined;
        const timer = window.setTimeout(() => setInstallSuccess(null), 10000);
        return () => window.clearTimeout(timer);
    }, [installSuccess]);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const payload = await requestMonitoringJson('/api/cluster/clusters');
                if (cancelled) return;
                const ready = (payload?.items || []).filter((cluster) => cluster.ready);
                setClusters(ready);
                setClustersError(null);
                if (ready.length === 0) {
                    setClusterId('');
                    return;
                }
                const preferred = initialClusterId && ready.some((cluster) => cluster.id === initialClusterId)
                    ? initialClusterId
                    : ready[0].id;
                setClusterId(preferred);
            } catch (error) {
                if (cancelled) return;
                setClusters([]);
                setClusterId('');
                setClustersError(error);
            }
        })();
        return () => { cancelled = true; };
    }, []);

    const applyNamespace = (event) => {
        event.preventDefault();
        const nextNamespace = namespaceInput.trim();
        if (nextNamespace && nextNamespace !== namespace) {
            setOperation(null);
            setOperationError(null);
            setInstallSuccess(null);
            setCompletedOperationId(null);
            setNamespace(nextNamespace);
        } else if (nextNamespace) {
            refreshStatus();
        }
    };

    const openClusterLink = async (kind) => {
        setOpeningLink(kind);
        setLinksError(null);
        try {
            const payload = await getClusterStackLinks(namespace, { clusterId });
            openMonitoringLink(payload, kind);
        } catch (error) {
            setLinksError(error.message || `Unable to open ${kind} dashboard`);
        } finally {
            setOpeningLink(null);
        }
    };

    const startOperation = (nextOperation) => {
        if (nextOperation.namespace && nextOperation.namespace !== namespace) {
            setNamespaceInput(nextOperation.namespace);
            setNamespace(nextOperation.namespace);
        }
        setOperation(nextOperation);
        setOperationError(null);
        setInstallSuccess(null);
        setCompletedOperationId(null);
        refreshStatus();
    };

    const openInstall = () => {
        setReinstallMode(false);
        setInstallOpen(true);
    };
    const openReinstall = () => {
        setReinstallMode(true);
        setInstallOpen(true);
    };

    const context = snapshot?.cluster?.context || 'Current kube context';
    const unhealthyStack = snapshot && ['absent', 'degraded', 'unreachable', 'unsupported'].includes(snapshot.status);
    const unhealthyDetails = snapshot?.components
        ?.filter((component) => ['degraded', 'missing'].includes(component.status))
        .map((component) => String(component.name || '').replace(/_/g, ' '))
        .filter(Boolean) || [];

    return (
        <ModulePage contentClassName="flex flex-col space-y-6">
                <ModuleHeader
                    icon={Webcam}
                    title="Observability"
                    description="Install and monitor the cluster observability stack powering metrics, dashboards, and accelerator profiling."
                    onToggleMobileNav={onToggleMobileNav}
                    actions={(
                        <>
                            <span className="hidden md:inline-flex items-center gap-1.5 rounded-md border border-slate-700/70 bg-slate-950/50 px-2.5 py-1.5 font-mono text-[10px] text-slate-300">
                                <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
                                {context}
                            </span>
                            <Button variant="secondary" size="icon" onClick={refreshStatus} disabled={status.loading} title="Refresh" aria-label="Refresh observability status">
                                <RefreshCw className={cn('h-4 w-4', status.loading && 'animate-spin')} />
                            </Button>
                        </>
                    )}
                />
                <section>
                    <div className="mb-4 flex flex-wrap items-start justify-between gap-3 border-b border-slate-800/80 pb-4">
                        <div
                            className="inline-flex flex-wrap items-center gap-1 rounded-2xl border border-slate-800/80 bg-slate-900/50 p-1.5 backdrop-blur-xl"
                            role="tablist"
                            aria-label="Observability service"
                        >
                            {SERVICE_TABS.map((tab) => {
                                const active = tab.value === activeServiceTab;
                                const Icon = tab.icon;
                                return (
                                    <button
                                        key={tab.value}
                                        type="button"
                                        role="tab"
                                        aria-selected={active}
                                        onClick={() => setActiveServiceTab(tab.value)}
                                        className={cn(
                                            'inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-semibold transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/50',
                                            active
                                                ? 'bg-sky-500/20 text-white shadow-[0_0_18px_rgba(56,189,248,0.18)] ring-1 ring-sky-400/30'
                                                : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-100'
                                        )}
                                    >
                                        <Icon className={cn('h-4 w-4', active ? 'text-white' : 'text-slate-500')} />
                                        {tab.label}
                                    </button>
                                );
                            })}
                        </div>

                        <div className="flex min-w-0 flex-col items-stretch gap-1 sm:items-end">
                            <div className="flex items-center gap-2.5 rounded-2xl border border-slate-800/80 bg-slate-900/50 p-1.5 pl-3 backdrop-blur-xl transition-colors hover:border-cyan-500/40">
                                <span className="inline-flex shrink-0 items-center gap-1.5 text-xs font-semibold tracking-wide text-cyan-400">
                                    <Upload size={14} />
                                    <span className="hidden sm:inline">Target cluster</span>
                                </span>
                                <Select
                                    id="observability-cluster"
                                    aria-label="Target cluster"
                                    value={clusterId}
                                    onChange={(event) => setClusterId(event.target.value)}
                                    disabled={clusters.length === 0}
                                    className="w-full min-w-0 sm:w-56"
                                >
                                    {clusters.length === 0 && <option value="">No ready clusters</option>}
                                    {clusters.map((cluster) => (
                                        <option key={cluster.id} value={cluster.id}>
                                            {cluster.name || cluster.id}
                                        </option>
                                    ))}
                                </Select>
                            </div>
                            {clustersError && (
                                <p className="flex items-center gap-1.5 text-[11px] text-red-300/90">
                                    <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                                    {clustersError.message || 'Unable to load clusters'}
                                </p>
                            )}
                        </div>
                    </div>
                </section>

                {activeServiceTab === 'cluster' && (
                    !clusterId ? (
                        <section className="relative overflow-hidden rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/80 via-slate-900/40 to-slate-950/90 p-5 shadow-xl backdrop-blur-xl">
                            <div className="relative">
                                <div className="mb-4">
                                    <SectionLabel tone="sky">Cluster infrastructure</SectionLabel>
                                </div>
                                <EmptyState
                                    icon={<Boxes className="h-10 w-10" />}
                                    title="No ready cluster selected"
                                    message="Cluster infrastructure observability requires a ready Kubernetes cluster. No status is shown until a ready cluster is available."
                                />
                            </div>
                        </section>
                    ) : (
                    <>
                <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                        <form onSubmit={applyNamespace} className="relative rounded-xl border border-slate-800/80 bg-slate-950/55 p-4 shadow-inner">
                            <div className="absolute right-3 top-3 text-cyan-400/20">
                                <Sparkles className="h-5 w-5" />
                            </div>
                            <SectionLabel tone="sky">Namespace</SectionLabel>
                            <div className="mt-3 flex items-center gap-3">
                                <Input
                                    id="monitoring-namespace"
                                    value={namespaceInput}
                                    onChange={(event) => setNamespaceInput(event.target.value)}
                                    placeholder={DEFAULT_NAMESPACE}
                                    className="flex-1"
                                />
                                <Button type="submit" variant="secondary" size="sm">Inspect</Button>
                            </div>
                        </form>
                        <div className="relative rounded-xl border border-slate-800/80 bg-slate-950/55 p-4 shadow-inner">
                            <SectionLabel tone="sky">GUIs</SectionLabel>
                            <div className="mt-3 flex flex-wrap items-center gap-2">
                                <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={() => openClusterLink('prometheus')}
                                    isLoading={openingLink === 'prometheus'}
                                    disabled={openingLink !== null}
                                >
                                    <Activity className="h-3.5 w-3.5 text-orange-300" /> Prometheus
                                </Button>
                                <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={() => openClusterLink('grafana')}
                                    isLoading={openingLink === 'grafana'}
                                    disabled={openingLink !== null}
                                >
                                    <BarChart3 className="h-3.5 w-3.5 text-blue-300" /> Grafana
                                </Button>
                                {linksError && (
                                    <span className="flex items-center gap-1.5 text-[11px] text-red-300/90">
                                        <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                                        {linksError}
                                    </span>
                                )}
                            </div>
                        </div>
                </div>

                {status.error && (
                    <div
                        role="alert"
                        aria-live="assertive"
                        className="relative overflow-hidden rounded-xl border border-red-500/40 bg-gradient-to-r from-red-950/70 via-red-950/40 to-slate-950 p-4 shadow-[0_0_30px_rgba(239,68,68,0.08)]"
                    >
                        <div className="flex items-start justify-between gap-4">
                            <div className="flex items-start gap-3">
                                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-red-500/30 bg-red-500/15">
                                    <AlertOctagon className="h-5 w-5 text-red-300" />
                                </span>
                                <div>
                                    <div className="text-sm font-bold text-red-200">Observability service is not responding normally</div>
                                    <p className="mt-1 text-xs leading-relaxed text-red-200/75">
                                        {status.error.message}
                                        {snapshot && ' The last successful snapshot is shown below and may be stale.'}
                                    </p>
                                </div>
                            </div>
                            <Button variant="secondary" size="sm" onClick={refreshStatus} isLoading={status.loading}>
                                <RefreshCw className="h-3.5 w-3.5" /> Retry
                            </Button>
                        </div>
                    </div>
                )}

                {installSuccess && (
                    <div
                        role="status"
                        aria-live="polite"
                        className="relative overflow-hidden rounded-xl border border-emerald-500/35 bg-gradient-to-r from-emerald-950/70 via-emerald-950/30 to-slate-950 p-4 shadow-[0_0_30px_rgba(16,185,129,0.1)]"
                    >
                        <div className="flex items-start justify-between gap-4">
                            <div className="flex items-start gap-3">
                                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-emerald-500/30 bg-emerald-500/15">
                                    <CheckCircle2 className="h-5 w-5 text-emerald-300" />
                                </span>
                                <div>
                                    <div className="text-sm font-bold text-emerald-200">
                                        Cluster observability stack installed successfully
                                    </div>
                                    <p className="mt-1 text-xs text-emerald-200/70">
                                        Prometheus and Grafana in <span className="font-mono">{installSuccess.namespace}</span> passed readiness verification.
                                    </p>
                                </div>
                            </div>
                            <button
                                type="button"
                                onClick={() => setInstallSuccess(null)}
                                className="rounded-lg p-1.5 text-emerald-300/60 transition-colors hover:bg-emerald-500/10 hover:text-emerald-200"
                                aria-label="Dismiss installation success message"
                            >
                                <X className="h-4 w-4" />
                            </button>
                        </div>
                    </div>
                )}

                {unhealthyStack && !status.error && (
                    <div
                        role="alert"
                        aria-live="polite"
                        className={`relative overflow-hidden rounded-xl border p-4 shadow-xl ${
                            snapshot.status === 'absent' || snapshot.status === 'unsupported'
                                ? 'border-amber-500/35 bg-gradient-to-r from-amber-950/60 via-amber-950/25 to-slate-950'
                                : 'border-red-500/40 bg-gradient-to-r from-red-950/70 via-red-950/35 to-slate-950'
                        }`}
                    >
                        <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-center">
                            <div className="flex items-start gap-3">
                                <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border ${
                                    snapshot.status === 'absent' || snapshot.status === 'unsupported'
                                        ? 'border-amber-500/30 bg-amber-500/15'
                                        : 'border-red-500/30 bg-red-500/15'
                                }`}>
                                    <AlertTriangle className={`h-5 w-5 ${
                                        snapshot.status === 'absent' || snapshot.status === 'unsupported'
                                            ? 'text-amber-300'
                                            : 'text-red-300'
                                    }`} />
                                </span>
                                <div>
                                    <div className="flex flex-wrap items-center gap-2">
                                        <span className="text-sm font-bold text-white">Observability stack is not fully operational</span>
                                        <Badge
                                            tone={snapshot.status === 'absent' || snapshot.status === 'unsupported' ? 'warning' : 'danger'}
                                            size="xs"
                                        >
                                            {snapshot.status}
                                        </Badge>
                                    </div>
                                    <p className="mt-1 text-xs leading-relaxed text-slate-300">{snapshot.message}</p>
                                    {unhealthyDetails.length > 0 && (
                                        <p className="mt-1 text-[10px] text-slate-500">
                                            Affected components: {unhealthyDetails.join(', ')}
                                        </p>
                                    )}
                                </div>
                            </div>
                            {snapshot.status === 'absent' && !operationId ? (
                                <Button variant="sky" size="sm" onClick={openInstall}>
                                    Install stack
                                </Button>
                            ) : snapshot.status === 'degraded' && !operationId ? (
                                <Button variant="sky" size="sm" onClick={openReinstall}>
                                    Reinstall
                                </Button>
                            ) : (
                                <Button variant="secondary" size="sm" onClick={refreshStatus} isLoading={status.loading}>
                                    <RefreshCw className="h-3.5 w-3.5" /> Recheck
                                </Button>
                            )}
                        </div>
                    </div>
                )}

                {status.loading && !snapshot ? (
                    <LoadingState label="Discovering observability infrastructure…" />
                ) : snapshot ? (
                    <>
                        <ClusterStackSummary snapshot={snapshot} />
                        <ClusterStackOperationProgress
                            operation={operation}
                            error={operationError}
                            onRetryPreflight={snapshot.status === 'absent' ? openInstall : null}
                        />
                        <ClusterStackComponentTable
                            components={snapshot.components}
                            actions={snapshot.status === 'ready' && !operationId ? (
                                <Button variant="sky" size="sm" onClick={openReinstall}>
                                    <RefreshCw className="h-3.5 w-3.5" /> Reinstall
                                </Button>
                            ) : null}
                        />

                        {snapshot.status === 'absent' && !operationId && (
                            <div className="relative overflow-hidden rounded-2xl border border-cyan-500/20 bg-gradient-to-br from-cyan-950/40 via-slate-900/80 to-slate-950 p-2 shadow-xl">
                                <EmptyState
                                    icon={<CloudCog className="h-10 w-10" />}
                                    title="Observability stack is not installed"
                                    message="Run mandatory preflight checks, review the installation plan, then install Prometheus and Grafana infrastructure."
                                    action={<Button variant="sky" onClick={openInstall}>Install observability stack</Button>}
                                />
                            </div>
                        )}

                        {snapshot.status === 'degraded' && (
                            <div className="relative overflow-hidden rounded-2xl border border-amber-500/20 bg-gradient-to-br from-amber-950/30 via-slate-900/80 to-slate-950 p-2 shadow-xl">
                                <EmptyState
                                    icon={<AlertTriangle className="h-10 w-10" />}
                                    title="Observability stack is degraded"
                                    message="Review component diagnostics, or reinstall the stack. Reinstalling removes the existing release and deletes stored metrics."
                                    action={<Button variant="sky" onClick={openReinstall}>Reinstall</Button>}
                                />
                            </div>
                        )}
                    </>
                ) : (
                    <div className="rounded-2xl border border-slate-800 bg-slate-900/70">
                        <EmptyState
                            title="Cluster status unavailable"
                            message="Check access to the current Kubernetes context and try again."
                            action={<Button variant="outline" onClick={refreshStatus}>Retry</Button>}
                        />
                    </div>
                )}
                    </>
                    )
                )}

                {activeServiceTab === 'accelerator' && (
                    <AcceleratorObservabilityPanel clusterId={clusterId} />
                )}

                {activeServiceTab === 'deployment' && (
                    profilingDeployment ? (
                        <DeploymentProfilingPanel
                            clusterId={clusterId}
                            deployment={profilingDeployment}
                            onBack={() => setProfilingDeployment(null)}
                        />
                    ) : (
                        <DeploymentObservabilityPanel
                            clusterId={clusterId}
                            onOpenProfiling={setProfilingDeployment}
                        />
                    )
                )}

            <InstallClusterStackModal
                isOpen={installOpen}
                namespace={namespace}
                clusterId={clusterId}
                reinstall={reinstallMode}
                onClose={() => setInstallOpen(false)}
                onOperationStarted={startOperation}
            />
        </ModulePage>
    );
}
