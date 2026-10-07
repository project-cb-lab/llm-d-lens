// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import { useMonitoringOperation } from './useMonitoringOperation';
import { openMonitoringLink } from './monitoringLinks';
import React, { useCallback, useEffect, useState } from 'react';
import {
    Activity,
    AlertOctagon,
    AlertTriangle,
    BarChart3,
    CheckCircle2,
    Gauge,
    RefreshCw,
    X,
} from 'lucide-react';
import { Button, EmptyState, LoadingState, SectionLabel, ToggleGroup } from '../ui';
import { ClusterStackComponentTable } from './ClusterStackComponentTable';
import { ClusterStackOperationProgress } from './ClusterStackOperationProgress';
import { InstallAcceleratorModal } from './InstallAcceleratorModal';
import {
    getAcceleratorCapabilities,
    getAcceleratorLinks,
    getAcceleratorOperation,
} from './acceleratorObservabilityBackend';
import { accessModeLabel } from './acceleratorLabels';
import { useAcceleratorObservability } from './useAcceleratorObservability';


function display(value) {
    return String(value || 'unknown').replace(/_/g, ' ');
}

export function AcceleratorObservabilityPanel({ clusterId }) {
    const [capabilities, setCapabilities] = useState([]);
    const [capabilitiesError, setCapabilitiesError] = useState(null);
    const [capabilitiesLoading, setCapabilitiesLoading] = useState(true);
    // Selected accelerator comes from the capabilities payload (every hardware
    // profile registers one); nothing is assumed about which vendor is present.
    const [activeType, setActiveType] = useState(null);
    const [namespace, setNamespace] = useState(null);
    const [accessMode, setAccessMode] = useState(null);
    const [installOpen, setInstallOpen] = useState(false);
    const [operation, setOperation] = useState(null);
    const [operationError, setOperationError] = useState(null);
    const [installSuccess, setInstallSuccess] = useState(null);
    const [completedOperationId, setCompletedOperationId] = useState(null);
    const [linksError, setLinksError] = useState(null);
    const [openingLink, setOpeningLink] = useState(null);

    const status = useAcceleratorObservability(activeType, namespace, accessMode, clusterId);
    const snapshot = status.data;
    const refreshStatus = status.refresh;
    const snapshotOperationId = snapshot?.active_operation_id;
    const operationId = operation?.operation_id
        || (snapshotOperationId !== completedOperationId ? snapshotOperationId : null);

    useEffect(() => {
        let cancelled = false;
        getAcceleratorCapabilities()
            .then((payload) => {
                if (cancelled) return;
                const accelerators = payload?.accelerators || [];
                setCapabilities(accelerators);
                if (accelerators.length) {
                    setActiveType((current) => (
                        current && accelerators.some((entry) => entry.type === current)
                            ? current
                            : accelerators[0].type
                    ));
                }
            })
            .catch((error) => {
                if (!cancelled) setCapabilitiesError(error);
            })
            .finally(() => {
                if (!cancelled) setCapabilitiesLoading(false);
            });
        return () => { cancelled = true; };
    }, []);

    // Keep the namespace in sync with the selected accelerator's default; a
    // running operation may temporarily point at a different one.
    useEffect(() => {
        const capability = capabilities.find((entry) => entry.type === activeType);
        if (capability?.default_namespace) setNamespace(capability.default_namespace);
    }, [activeType, capabilities]);

    const loadOperation = useCallback((id, options) => getAcceleratorOperation(activeType, id, options), [activeType]);
    useMonitoringOperation({
        operationId, operationStatus: operation?.status, completedOperationId, namespace,
        loadOperation: loadOperation, refreshStatus, setOperation, setOperationError,
        setCompletedOperationId, setInstallSuccess,
    });

    useEffect(() => {
        if (!installSuccess) return undefined;
        const timer = window.setTimeout(() => setInstallSuccess(null), 10000);
        return () => window.clearTimeout(timer);
    }, [installSuccess]);

    const selectAccelerator = (type) => {
        const capability = capabilities.find((entry) => entry.type === type);
        setActiveType(type);
        setNamespace(capability?.default_namespace || '');
        setAccessMode(null);
        setOperation(null);
        setOperationError(null);
        setInstallSuccess(null);
        setCompletedOperationId(null);
    };

    const startOperation = (nextOperation) => {
        if (nextOperation.namespace && nextOperation.namespace !== namespace) {
            setNamespace(nextOperation.namespace);
        }
        setOperation(nextOperation);
        setOperationError(null);
        setInstallSuccess(null);
        setCompletedOperationId(null);
        refreshStatus();
    };

    const openLink = async (kind) => {
        setOpeningLink(kind);
        setLinksError(null);
        try {
            const payload = await getAcceleratorLinks(activeType, { clusterId });
            openMonitoringLink(payload, kind);
        } catch (error) {
            setLinksError(error.message || `Unable to open ${kind} dashboard`);
        } finally {
            setOpeningLink(null);
        }
    };

    const capability = capabilities.find((entry) => entry.type === activeType) || null;
    const displayName = capability?.display_name || display(activeType);
    const releaseName = capability?.release_name || displayName;

    const accessModes = (snapshot?.access_modes || []).map((entry) => ({
        value: entry.mode,
        label: accessModeLabel(entry.mode),
        disabled: !entry.available,
    }));
    // Prefer the mode actually detected on the cluster over a hardcoded default:
    // an accelerator that only ships a device plugin (or only a DRA driver)
    // shouldn't surface the other mode's "deploy it first" hint on open.
    const firstAvailableMode = (snapshot?.access_modes || []).find((entry) => entry.available)?.mode;
    const effectiveAccessMode = accessMode
        || snapshot?.access_mode
        || firstAvailableMode
        || snapshot?.access_modes?.[0]?.mode
        || 'dra';
    const unavailableMessages = (snapshot?.access_modes || [])
        .filter((entry) => entry.mode === effectiveAccessMode && !entry.available && entry.message)
        .map((entry) => entry.message);
    const canInstall = !operationId && ['absent', 'degraded', 'ready'].includes(snapshot?.status);

    // Accelerator tabs come entirely from the backend capabilities registry
    // (which now lists every registered provider); no hardcoded placeholders.
    const tabOptions = capabilities.map((entry) => ({
        value: entry.type,
        label: entry.display_name,
    }));

    if (!clusterId) {
        return (
            <div>
                <EmptyState
                    icon={<Gauge className="h-10 w-10" />}
                    title="No ready cluster selected"
                    message="Accelerator observability requires a ready Kubernetes cluster. No status is shown until a ready cluster is available."
                />
            </div>
        );
    }

    return (
        <div>
            <div className="mb-4" role="tablist" aria-label="Accelerator type">
                <ToggleGroup options={tabOptions} value={activeType} onChange={selectAccelerator} activeClassName="bg-sky-500/20 dark:bg-sky-500/20 text-white ring-1 ring-sky-400/30 shadow-none" />
            </div>

            {capabilitiesError && (
                <div className="rounded-xl border border-red-500/40 bg-red-950/60 p-4 text-xs text-red-200">
                    {capabilitiesError.message || 'Unable to load accelerator capabilities'}
                </div>
            )}

            {capabilitiesLoading ? (
                    <LoadingState label="Loading accelerators…" />
                ) : status.loading && !snapshot ? (
                    <LoadingState label="Discovering accelerator observability…" />
                ) : status.error ? (
                    <div className="rounded-xl border border-red-500/40 bg-red-950/60 p-4">
                        <div className="flex items-start justify-between gap-4">
                            <div className="flex items-start gap-3">
                                <AlertOctagon className="mt-0.5 h-5 w-5 shrink-0 text-red-300" />
                                <div>
                                    <div className="text-sm font-bold text-red-200">Accelerator observability service is not responding normally</div>
                                    <p className="mt-1 text-xs text-red-200/75">{status.error.message}</p>
                                </div>
                            </div>
                            <Button variant="secondary" size="sm" onClick={refreshStatus} isLoading={status.loading}>
                                <RefreshCw className="h-3.5 w-3.5" /> Retry
                            </Button>
                        </div>
                    </div>
                ) : snapshot ? (
                    <>
                        {snapshot.status !== 'ready' && (
                            <div className="rounded-xl border border-slate-800/80 bg-slate-950/55 p-4">
                                <p className="text-xs leading-relaxed text-slate-400">
                                    {snapshot.message || `${displayName} telemetry collected through the ${releaseName} DaemonSet.`}
                                </p>
                            </div>
                        )}

                        <div className="mt-4">
                            <SectionLabel tone="sky">GPU access mode</SectionLabel>
                            <p className="mt-1 text-xs text-slate-500">
                                Detected from the environment. Choose the access mode used to expose {displayName} metrics.
                            </p>
                            <div className="mt-3">
                                <ToggleGroup
                                    options={accessModes}
                                    value={effectiveAccessMode}
                                    onChange={(mode) => setAccessMode(mode)}
                                    fullWidth
                                    activeClassName="bg-sky-500/20 dark:bg-sky-500/20 text-white ring-1 ring-sky-400/30 shadow-none"
                                />
                                {unavailableMessages.length > 0 && (
                                    <div className="mt-2 space-y-1.5">
                                        {unavailableMessages.map((message, index) => (
                                            <div key={`${message}-${index}`} className="flex items-start gap-2 text-[11px] text-amber-300/90">
                                                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                                                <span>{message}</span>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        </div>

                        <div className="relative mt-4 rounded-xl border border-slate-800/80 bg-slate-950/55 p-4 shadow-inner">
                            <SectionLabel tone="sky">Observability UI</SectionLabel>
                            {snapshot.cluster_reachable !== false && (
                                <div className="mt-3 flex flex-wrap items-center gap-2">
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={() => openLink('prometheus')}
                                        isLoading={openingLink === 'prometheus'}
                                        disabled={openingLink !== null}
                                    >
                                        <Activity className="h-3.5 w-3.5 text-orange-300" /> Prometheus
                                    </Button>
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={() => openLink('grafana')}
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
                            )}
                        </div>

                        {installSuccess && (
                            <div className="mt-3 flex items-start justify-between gap-3 rounded-xl border border-emerald-500/35 bg-emerald-950/50 p-4">
                                <div className="flex items-start gap-3">
                                    <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-emerald-300" />
                                    <div>
                                        <div className="text-sm font-bold text-emerald-200">{displayName} observability installed successfully</div>
                                        <p className="mt-1 text-xs text-emerald-200/70">
                                            {releaseName} in <span className="font-mono">{installSuccess.namespace}</span> passed readiness verification.
                                        </p>
                                    </div>
                                </div>
                                <button type="button" onClick={() => setInstallSuccess(null)} className="rounded-lg p-1.5 text-emerald-300/60 hover:bg-emerald-500/10" aria-label="Dismiss">
                                    <X className="h-4 w-4" />
                                </button>
                            </div>
                        )}

                        <div className="mt-4">
                            <ClusterStackOperationProgress
                                operation={operation}
                                error={operationError}
                                onRetryPreflight={canInstall ? () => setInstallOpen(true) : null}
                            />
                        </div>

                        <div className="mt-4">
                            <ClusterStackComponentTable
                                components={snapshot.components}
                                actions={snapshot.status === 'ready' && !operationId ? (
                                    <Button variant="sky" size="sm" onClick={() => setInstallOpen(true)}>
                                        <RefreshCw className="h-3.5 w-3.5" /> Reinstall
                                    </Button>
                                ) : null}
                            />
                        </div>

                        {snapshot.status === 'absent' && !operationId && (
                            <div className="mt-4 rounded-xl border border-emerald-500/20 bg-emerald-950/30 p-2">
                                <EmptyState
                                    icon={<Gauge className="h-10 w-10" />}
                                    title={`${displayName} observability is not installed`}
                                    message={`Run preflight checks, pick a detected GPU access mode, then install the ${releaseName} chart. Prometheus and Grafana reuse the existing Cluster infrastructure stack.`}
                                    action={<Button onClick={() => setInstallOpen(true)}>Install accelerator observability</Button>}
                                />
                            </div>
                        )}

                        {snapshot.status === 'degraded' && (
                            <div className="mt-4 rounded-xl border border-amber-500/20 bg-amber-950/30 p-2">
                                <EmptyState
                                    icon={<AlertTriangle className="h-10 w-10" />}
                                    title={`${displayName} observability is incomplete`}
                                    message={`Some ${releaseName} resources are missing. Run preflight and reinstall to reconcile the chart; Prometheus and Grafana reuse the existing Cluster infrastructure stack.`}
                                    action={(
                                        <div className="flex items-center justify-center gap-2">
                                            <Button onClick={() => setInstallOpen(true)}>Repair / reinstall</Button>
                                            <Button variant="outline" onClick={refreshStatus}>Refresh status</Button>
                                        </div>
                                    )}
                                />
                            </div>
                        )}
                    </>
                ) : (
                    <EmptyState
                        title="Accelerator status unavailable"
                        message="Check access to the current Kubernetes context and try again."
                        action={<Button variant="outline" onClick={refreshStatus}>Retry</Button>}
                    />
                )}

            <InstallAcceleratorModal
                isOpen={installOpen}
                accelerator={activeType}
                defaultNamespace={namespace}
                status={snapshot}
                supportedAccessModes={capability?.access_modes || []}
                initialAccessMode={accessMode}
                clusterId={clusterId}
                onClose={() => setInstallOpen(false)}
                onOperationStarted={startOperation}
            />
        </div>
    );
}
