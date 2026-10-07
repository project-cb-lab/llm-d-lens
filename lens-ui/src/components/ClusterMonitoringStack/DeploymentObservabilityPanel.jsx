// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Activity, AlertTriangle, BarChart3, Boxes, CheckCircle2, ChevronDown, GitBranch, Loader2, Pause, Play, RefreshCw, Rocket, Trash2 } from 'lucide-react';
import { Badge, Button, EmptyState, LoadingState, SectionLabel } from '../ui';
import {
    getDeploymentExecutions,
} from '../OptimizationWorkspace/remoteDeployBackend';
import {
    getClusterDeploymentMonitoring,
    getClusterStackLinks,
    manageDeploymentMonitoring,
} from './clusterMonitoringStackBackend';

const CENTRAL_NAMESPACE = 'llm-d-monitoring';
const GRAFANA_PERFORMANCE_DASHBOARD = '/d/llm-d-performance/llm-d-performance-dashboard';

function monitoringChip(status) {
    switch (status) {
        case 'enabled':
            return { status: 'ready', label: 'Enabled' };
        case 'disabled':
            return { status: 'inactive', label: 'Disabled' };
        case 'absent':
            return { status: 'absent', label: 'Not installed' };
        case 'unreachable':
            return { status: 'unreachable', label: 'Unreachable' };
        default:
            return { status: 'neutral', label: status || 'Unknown' };
    }
}

function MonitoringMenuItem({ onClick, disabled, icon, label, danger = false }) {
    return (
        <button
            type="button"
            role="menuitem"
            onClick={onClick}
            disabled={disabled}
            className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                danger ? 'text-rose-400 hover:bg-rose-500/10' : 'text-slate-200 hover:bg-slate-800'
            }`}
        >
            {icon}
            {label}
        </button>
    );
}

const MONITORING_TONE_CLASSES = {
    ready: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20',
    inactive: 'border-slate-700 bg-slate-800 text-slate-400 hover:bg-slate-800/60',
    absent: 'border-amber-500/40 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20',
    unreachable: 'border-red-500/40 bg-red-500/10 text-red-300 hover:bg-red-500/20',
    neutral: 'border-slate-700 bg-slate-800 text-slate-400 hover:bg-slate-800/60',
};

export function DeploymentObservabilityPanel({ clusterId, onOpenProfiling }) {
    const [deployments, setDeployments] = useState([]);
    const [monitoring, setMonitoring] = useState({ items: [] });
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [actionKey, setActionKey] = useState('');
    const [actionError, setActionError] = useState('');
    const [openingLink, setOpeningLink] = useState(null);
    const [linksError, setLinksError] = useState('');
    const [openMenu, setOpenMenu] = useState(null);

    useEffect(() => {
        if (!clusterId) {
            setDeployments([]);
            setMonitoring({ items: [] });
            setLoading(false);
            return undefined;
        }
        let cancelled = false;
        const poll = async () => {
            try {
                const [deps, mon] = await Promise.all([
                    getDeploymentExecutions({ status: 'ready', clusterId }),
                    getClusterDeploymentMonitoring(clusterId),
                ]);
                if (cancelled) return;
                setDeployments(Array.isArray(deps) ? deps : []);
                setMonitoring(mon || { items: [] });
                setError(null);
            } catch (nextError) {
                if (!cancelled) setError(nextError.message);
            } finally {
                if (!cancelled) setLoading(false);
            }
        };
        poll();
        const interval = window.setInterval(poll, 10000);
        return () => { cancelled = true; window.clearInterval(interval); };
    }, [clusterId]);

    const readyDeployments = deployments;
    const monitoringByDeployment = (monitoring.items || []).reduce((acc, item) => {
        acc[item.execution_id] = item;
        return acc;
    }, {});
    const clusterReachable = monitoring.cluster_reachable !== false;
    const stackReady = monitoring.stack_ready === true;
    const actionsAllowed = clusterReachable && stackReady;

    const runAction = async (deployment, action) => {
        const key = `${deployment.execution_id}:${action}`;
        setActionKey(key);
        setActionError('');
        try {
            await manageDeploymentMonitoring(deployment.execution_id, action, { clusterId });
            const payload = await getClusterDeploymentMonitoring(clusterId);
            setMonitoring(payload || { items: [] });
        } catch (nextError) {
            setActionError(nextError.message);
        } finally {
            setActionKey((current) => (current === key ? '' : current));
        }
    };

    const openStackLink = async (kind, deployment) => {
        setOpeningLink(kind);
        setLinksError('');
        try {
            const payload = await getClusterStackLinks(CENTRAL_NAMESPACE, { clusterId });
            const link = (payload?.links || []).find((entry) => entry.kind === kind);
            if (!link?.available || !link.local_port) {
                setLinksError(link?.message || `${kind} dashboard is not available`);
                return;
            }
            const host = window.location.hostname;
            let target = `http://${host}:${link.local_port}`;
            if (kind === 'grafana') {
                const path = link.dashboard_path || GRAFANA_PERFORMANCE_DASHBOARD;
                const params = new URLSearchParams();
                if (deployment?.namespace) params.set('var-namespace', deployment.namespace);
                const query = params.toString();
                target = `http://${host}:${link.local_port}${path}${query ? `?${query}` : ''}`;
            } else if (link.dashboard_path) {
                target = `http://${host}:${link.local_port}${link.dashboard_path}`;
            }
            window.open(target, '_blank', 'noopener,noreferrer');
        } catch (nextError) {
            setLinksError(nextError.message || `Unable to open ${kind} dashboard`);
        } finally {
            setOpeningLink(null);
        }
    };

    if (loading) {
        return <LoadingState label="Discovering deployment monitoring…" />;
    }

    if (!clusterId) {
        return (
            <section className="relative overflow-hidden rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/80 via-slate-900/40 to-slate-950/90 p-5 shadow-xl backdrop-blur-xl">
                <div className="relative">
                    <div className="mb-4">
                        <SectionLabel tone="sky">Deployment monitoring</SectionLabel>
                    </div>
                    <EmptyState
                        icon={<Boxes className="h-10 w-10" />}
                        title="No ready cluster selected"
                        message="Select a cluster to manage per-deployment observability."
                    />
                </div>
            </section>
        );
    }

    if (error) {
        return (
            <section className="relative overflow-hidden rounded-2xl border border-red-500/40 bg-gradient-to-br from-red-950/70 via-red-950/40 to-slate-950 p-5 shadow-xl backdrop-blur-xl">
                <div className="relative">
                    <div className="mb-4">
                        <SectionLabel tone="sky">Deployment monitoring</SectionLabel>
                    </div>
                    <div className="flex items-start gap-3 rounded-xl border border-red-500/30 bg-red-500/10 p-4">
                        <AlertTriangle className="h-5 w-5 shrink-0 text-red-300" />
                        <div className="flex-1">
                            <div className="text-sm font-bold text-red-200">Unable to load deployment monitoring</div>
                            <p className="mt-1 text-xs text-red-200/75">{error}</p>
                        </div>
                        <Button variant="secondary" size="sm" onClick={() => { setLoading(true); }}>Retry</Button>
                    </div>
                </div>
            </section>
        );
    }

    return (
        <section className="relative overflow-hidden rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/80 via-slate-900/40 to-slate-950/90 p-5 shadow-xl backdrop-blur-xl">
            <div className="relative">
                <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                    <div>
                        <SectionLabel tone="sky">Deployment monitoring</SectionLabel>
                        <p className="mt-1 text-xs text-slate-500">
                            Per-namespace ServiceMonitors and PodMonitors feed the central Prometheus stack.
                        </p>
                    </div>
                    {readyDeployments.length > 0 && (
                        <Badge tone="success" size="sm">{readyDeployments.length} ready deployment{readyDeployments.length === 1 ? '' : 's'}</Badge>
                    )}
                </div>

                {!clusterReachable && readyDeployments.length > 0 && (
                    <div role="status" className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
                        The cluster is unreachable, so monitoring resources cannot be managed right now.
                    </div>
                )}
                {clusterReachable && !stackReady && readyDeployments.length > 0 && (
                    <div role="status" className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
                        The cluster monitoring stack is not installed, so deployment metrics cannot be scraped yet.
                        Install it from the Cluster tab before enabling deployment monitoring.
                    </div>
                )}

                {actionError && (
                    <div role="alert" className="mb-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-400">
                        {actionError}
                    </div>
                )}

                {linksError && (
                    <div role="alert" className="mb-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-400">
                        {linksError}
                    </div>
                )}

                {!readyDeployments.length ? (
                    <EmptyState
                        className="bg-slate-900/40 border border-slate-800/80 rounded-2xl backdrop-blur-sm mt-2"
                        icon={
                            <div className="p-3 bg-sky-500/10 border border-sky-500/20 text-sky-400 rounded-full">
                                <Rocket className="w-8 h-8" />
                            </div>
                        }
                        title="No ready deployments"
                        message="Ready deployments for this cluster will appear here once a deploy completes."
                    />
                ) : (
                    <div className="mt-2 overflow-x-auto rounded-xl border border-slate-800/60">
                        <table className="w-full min-w-[40rem] text-left text-sm">
                            <thead className="bg-slate-950/60 text-slate-400">
                                <tr>
                                    <th className="px-5 py-3 text-[10px] font-semibold uppercase tracking-wider">Deployment</th>
                                    <th className="px-5 py-3 text-[10px] font-semibold uppercase tracking-wider">Description</th>
                                    <th className="px-5 py-3 text-[10px] font-semibold uppercase tracking-wider">Namespace</th>
                                    <th className="px-5 py-3 text-[10px] font-semibold uppercase tracking-wider">Monitoring</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-800/70">
                                {readyDeployments.map((deployment) => {
                                    const mon = monitoringByDeployment[deployment.execution_id] || {};
                                    const chip = monitoringChip(mon.status);
                                    const key = `${deployment.execution_id}:`;
                                    const installEnabled = actionsAllowed && mon.status !== 'enabled';
                                    return (
                                        <tr key={deployment.execution_id} className="transition-colors hover:bg-slate-800/40">
                                            <td className="px-5 py-3 font-mono text-xs text-slate-200">{deployment.display_name || deployment.name || deployment.guide || '—'}</td>
                                            <td className="px-5 py-3 max-w-xs truncate text-xs text-slate-400">{deployment.description || <span className="text-slate-500">—</span>}</td>
                                            <td className="px-5 py-3 font-mono text-xs text-slate-400">{deployment.namespace || '—'}</td>
                                            <td className="px-5 py-3">
                                                <div className="relative inline-block">
                                                        <button
                                                            type="button"
                                                            onClick={(event) => {
                                                                const rect = event.currentTarget.getBoundingClientRect();
                                                                setOpenMenu((current) => (current?.key === key ? null : { key, top: rect.bottom, right: window.innerWidth - rect.right }));
                                                            }}
                                                            disabled={!!actionKey || !!openingLink}
                                                            className={`inline-flex w-40 items-center gap-1.5 rounded border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${MONITORING_TONE_CLASSES[chip.status] || MONITORING_TONE_CLASSES.neutral}`}
                                                            aria-haspopup="menu"
                                                            aria-expanded={openMenu?.key === key}
                                                        >
                                                            <span className="w-1.5 h-1.5 rounded-full bg-current shrink-0" aria-hidden="true" />
                                                            <span className="flex-1 whitespace-nowrap text-left">{chip.label}</span>
                                                            {actionKey.startsWith(key) || openingLink ? <Loader2 size={12} className="animate-spin opacity-70" /> : <ChevronDown size={12} className="opacity-70" />}
                                                        </button>
                                                        {openMenu?.key === key && createPortal(
                                                            <>
                                                                <div className="fixed inset-0 z-[290]" onClick={() => setOpenMenu(null)} />
                                                                <div className="fixed z-[300] w-44 overflow-hidden rounded-lg border border-slate-700/70 bg-slate-900 shadow-xl" style={{ top: openMenu.top + 6, right: openMenu.right }} role="menu">
                                                                    {mon.status === 'enabled' && (
                                                                        <>
                                                                            <MonitoringMenuItem
                                                                                onClick={() => { setOpenMenu(null); openStackLink('grafana', deployment); }}
                                                                                disabled={!!openingLink}
                                                                                icon={<BarChart3 size={14} className="shrink-0 text-blue-300" />}
                                                                                label="Open Grafana"
                                                                            />
                                                                            <MonitoringMenuItem
                                                                                onClick={() => { setOpenMenu(null); openStackLink('prometheus', deployment); }}
                                                                                disabled={!!openingLink}
                                                                                icon={<Activity size={14} className="shrink-0 text-orange-300" />}
                                                                                label="Open Prometheus"
                                                                            />
                                                                            <div className="h-px bg-slate-700/70" />
                                                                            <MonitoringMenuItem
                                                                                onClick={() => { setOpenMenu(null); onOpenProfiling?.(deployment); }}
                                                                                disabled={!!openingLink}
                                                                                icon={<GitBranch size={14} className="shrink-0 text-sky-300" />}
                                                                                label="Profiling"
                                                                            />
                                                                            <div className="h-px bg-slate-700/70" />
                                                                            <MonitoringMenuItem
                                                                                onClick={() => { setOpenMenu(null); runAction(deployment, 'disable'); }}
                                                                                disabled={!!actionKey}
                                                                                icon={<Pause size={14} className="shrink-0 text-slate-300" />}
                                                                                label="Disable"
                                                                            />
                                                                            <div className="h-px bg-slate-700/70" />
                                                                            <MonitoringMenuItem
                                                                                onClick={() => { setOpenMenu(null); runAction(deployment, 'uninstall'); }}
                                                                                disabled={!!actionKey}
                                                                                icon={<Trash2 size={14} className="shrink-0 text-rose-400" />}
                                                                                label="Uninstall"
                                                                                danger
                                                                            />
                                                                        </>
                                                                    )}
                                                                    {mon.status === 'disabled' && (
                                                                        <>
                                                                            <MonitoringMenuItem
                                                                                onClick={() => { setOpenMenu(null); runAction(deployment, 'enable'); }}
                                                                                disabled={!installEnabled || !!actionKey}
                                                                                icon={<Play size={14} className="shrink-0 text-emerald-400" />}
                                                                                label="Enable"
                                                                            />
                                                                            <div className="h-px bg-slate-700/70" />
                                                                            <MonitoringMenuItem
                                                                                onClick={() => { setOpenMenu(null); runAction(deployment, 'uninstall'); }}
                                                                                disabled={!!actionKey}
                                                                                icon={<Trash2 size={14} className="shrink-0 text-rose-400" />}
                                                                                label="Uninstall"
                                                                                danger
                                                                            />
                                                                        </>
                                                                    )}
                                                                    {mon.status !== 'enabled' && mon.status !== 'disabled' && (
                                                                        <MonitoringMenuItem
                                                                            onClick={() => { setOpenMenu(null); runAction(deployment, 'install'); }}
                                                                            disabled={!installEnabled || !!actionKey}
                                                                            icon={<Rocket size={14} className="shrink-0 text-cyan-400" />}
                                                                            label="Install"
                                                                        />
                                                                    )}
                                                                </div>
                                                            </>,
                                                            document.body
                                                        )}
                                                    </div>
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                )}

                <div className="mt-4 flex items-center justify-between gap-3">
                    <span className="flex items-center gap-1.5 text-[10px] text-slate-500">
                        <RefreshCw className="h-3 w-3" /> Status refreshes automatically every 10 seconds.
                    </span>
                    {monitoring.stack_ready && (
                        <span className="flex items-center gap-1.5 text-[10px] text-emerald-400">
                            <CheckCircle2 className="h-3 w-3" /> Central Prometheus stack ready
                        </span>
                    )}
                </div>
            </div>
        </section>
    );
}
