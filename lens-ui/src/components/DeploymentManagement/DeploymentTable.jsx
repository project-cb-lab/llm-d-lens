import { useClipboard } from '../../hooks/useClipboard';
import { Fragment, useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, ChevronRight, Copy, FileText, Info, Link2, Loader2, Pencil, Share2, Trash2 } from 'lucide-react';
import {
    deploymentInternalEndpoint,
    deploymentKind,
    deploymentLocalEndpoint,
    deploymentTitle,
    effectiveStatusGroup,
    effectiveStatusLabel,
    statusDotClassForGroup,
    statusGroup,
} from './deploymentPresentation';
import { getDeploymentExecutionPods } from '../OptimizationWorkspace/remoteDeployBackend';
import { DeploymentMonitoringButton } from './DeploymentMonitoringButton';
import { PodStatusBadge } from './PodStatusBadge';

const PODS_POLL_INTERVAL_MS = 5000;

function StatusDot({ item, pods }) {
    if (item) {
        const group = effectiveStatusGroup(item, pods);
        const label = effectiveStatusLabel(item, pods);
        return (
            <span
                className={`h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s] ${statusDotClassForGroup(group)}`}
                title={label}
                aria-label={label}
            />
        );
    }
    return null;
}

function CopyButton({ value, copied, onCopy }) {
    if (!value) return null;
    return (
        <button
            type="button"
            onClick={() => onCopy(value)}
            className="inline-flex shrink-0 items-center gap-1 rounded-md border border-slate-700 px-2 py-1 text-[10px] font-medium text-slate-300 transition-colors hover:border-emerald-500/40 hover:text-slate-100"
            title="Copy endpoint"
        >
            {copied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
        </button>
    );
}

function EndpointCell({ value, copiedEndpoint, onCopy, emptyLabel = '—' }) {
    if (!value) return <span className="text-slate-600">{emptyLabel}</span>;
    return (
        <div className="flex items-center gap-2">
            <span className="min-w-0 truncate font-mono text-[11px] text-slate-400" title={value}>{value}</span>
            <CopyButton value={value} copied={copiedEndpoint === value} onCopy={onCopy} />
        </div>
    );
}

function RowActions({ item, busy, connecting, onInspect, onViewPodsLogs, onEdit, onDelete, onNavigate, onConnectLocalEndpoint, onShare }) {
    const name = deploymentTitle(item);
    const isInProgress = statusGroup(item.status) === 'In progress';
    const localEndpoint = deploymentLocalEndpoint(item);
    const canConnect = !localEndpoint && !item.pending && item.status === 'ready' && item.endpoint;
    return (
        <div className="flex items-center justify-end gap-1">
            <>
                {busy && <Loader2 size={14} className="mr-1 animate-spin text-cyan-300" aria-hidden="true" />}
                {canConnect && (
                    <button
                        type="button"
                        onClick={() => onConnectLocalEndpoint?.(item)}
                        disabled={busy || connecting}
                        aria-label={`Connect local endpoint for ${name}`}
                        title="Create local port-forward"
                        className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-800/70 hover:text-cyan-200 disabled:opacity-40"
                    >
                        {connecting ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <Link2 size={14} />}
                    </button>
                )}
                <button
                    type="button"
                    onClick={() => onInspect(item)}
                    disabled={busy}
                    aria-label={`View details of ${name}`}
                    title="View details"
                    className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-800/70 hover:text-slate-100 disabled:opacity-40"
                >
                    <Info size={14} />
                </button>
                {!item.pending && <button
                    type="button"
                    onClick={() => onEdit(item)}
                    disabled={busy}
                    aria-label={`Edit description of ${name}`}
                    title="Edit name and description"
                    className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-800/70 hover:text-slate-100 disabled:opacity-40"
                >
                    <Pencil size={14} />
                </button>}
                <button
                    type="button"
                    onClick={() => onViewPodsLogs?.(item)}
                    disabled={busy}
                    aria-label={`View pods and logs of ${name}`}
                    title="View Pods & Logs"
                    className={`rounded-lg p-1.5 transition disabled:opacity-40 ${
                        isInProgress
                            ? 'text-sky-400 bg-sky-500/10 border border-sky-500/30 animate-pulse hover:bg-sky-500/20'
                            : 'text-slate-400 hover:bg-slate-800/70 hover:text-slate-100'
                    }`}
                >
                    <FileText size={14} />
                </button>
                {!item.pending && <DeploymentMonitoringButton deployment={item} onNavigate={onNavigate} />}
                {!item.pending && onShare && <button
                    type="button"
                    onClick={() => onShare(item)}
                    disabled={busy}
                    aria-label={`Share ${name}`}
                    title="Share deployment"
                    className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-800/70 hover:text-violet-200 disabled:opacity-40"
                >
                    <Share2 size={14} />
                </button>}
                {!item.pending && <button
                    type="button"
                    onClick={() => onDelete(item)}
                    disabled={busy}
                    aria-label={`Delete ${name}`}
                    title="Delete deployment"
                    className="rounded-lg p-1.5 text-slate-400 transition hover:bg-rose-500/10 hover:text-rose-300 disabled:opacity-40"
                >
                    <Trash2 size={14} />
                </button>}
            </>
        </div>
    );
}

function PodsExpandPanel({ pods, loading, error }) {
    if (loading && pods === undefined) {
        return (
            <div className="flex items-center gap-2 px-4 py-3 text-[11px] text-slate-500">
                <Loader2 size={12} className="animate-spin" aria-hidden="true" /> Loading pods…
            </div>
        );
    }
    const list = pods || [];
    return (
        <div className="px-4 py-3">
            {error && <p className="mb-2 text-[11px] text-rose-300">{error}</p>}
            {list.length === 0 ? (
                <p className="text-[11px] text-slate-500">No pods found in namespace.</p>
            ) : (
                <table className="w-full min-w-[40rem] border-collapse text-left text-[11px]">
                    <thead className="text-slate-500">
                        <tr>
                            <th scope="col" className="py-1 pl-10 pr-3 font-medium">Pod</th>
                            <th scope="col" className="py-1 pr-3 font-medium">Status</th>
                            <th scope="col" className="py-1 pr-3 font-medium">Restarts</th>
                            <th scope="col" className="py-1 pr-3 font-medium">Node</th>
                            <th scope="col" className="py-1 pr-3 font-medium">Pod IP</th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/40">
                        {list.map((pod) => (
                            <tr key={pod.name}>
                                <td className="py-1 pl-10 pr-3 font-mono text-slate-300">{pod.name}</td>
                                <td className="py-1 pr-3">
                                    <div className="flex items-center gap-1.5 text-slate-300">
                                        <PodStatusBadge phase={pod.phase} reason={pod.status_reason} ready={pod.ready} />
                                        <span>{pod.status_reason || pod.phase || 'Unknown'}</span>
                                    </div>
                                </td>
                                <td className="py-1 pr-3 text-slate-400">{pod.restarts ?? 0}</td>
                                <td className="py-1 pr-3 text-slate-400">{pod.node_name || '—'}</td>
                                <td className="py-1 pr-3 font-mono text-slate-500">{pod.pod_ip || '—'}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}
        </div>
    );
}

function ExpandToggle({ expanded, onToggle, name }) {
    return (
        <button
            type="button"
            onClick={onToggle}
            aria-expanded={expanded}
            aria-label={expanded ? `Hide pods for ${name}` : `Show pods for ${name}`}
            title={expanded ? 'Hide pods' : 'Show pods'}
            className="shrink-0 rounded p-0.5 text-slate-500 transition hover:bg-slate-800/70 hover:text-slate-200"
        >
            {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </button>
    );
}

export function DeploymentTable({ items, mutatingId, connectingEndpointId = '', clusterNameById = {}, onInspect, onViewPodsLogs, onEdit, onDelete, onNavigate, onConnectLocalEndpoint, onShare }) {
    const { copiedValue: copiedEndpoint, copy: copyEndpoint } = useClipboard();
    const [expandedId, setExpandedId] = useState('');
    const [podsById, setPodsById] = useState({});
    const [podsLoadingId, setPodsLoadingId] = useState('');
    const [podsErrorById, setPodsErrorById] = useState({});
    const pollTimerRef = useRef(null);

    const fetchPodsFor = async (executionId, silent = false) => {
        if (!executionId) return;
        if (!silent) setPodsLoadingId(executionId);
        try {
            const data = await getDeploymentExecutionPods(executionId);
            const list = Array.isArray(data?.pods) ? data.pods : [];
            setPodsById((current) => ({ ...current, [executionId]: list }));
            setPodsErrorById((current) => ({ ...current, [executionId]: data?.error || '' }));
        } catch (err) {
            setPodsErrorById((current) => ({ ...current, [executionId]: err.message || 'Failed to fetch pods' }));
        } finally {
            if (!silent) setPodsLoadingId((current) => (current === executionId ? '' : current));
        }
    };

    const toggleExpand = (executionId) => {
        setExpandedId((current) => {
            const next = current === executionId ? '' : executionId;
            if (next) fetchPodsFor(next);
            return next;
        });
    };

    // Keep the expanded row's pod list live while it's open; pause entirely
    // once collapsed so we don't poll pods for every deployment on the page.
    useEffect(() => {
        if (pollTimerRef.current) clearInterval(pollTimerRef.current);
        if (!expandedId) return undefined;
        pollTimerRef.current = setInterval(() => fetchPodsFor(expandedId, true), PODS_POLL_INTERVAL_MS);
        return () => clearInterval(pollTimerRef.current);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [expandedId]);

    return (
        <>
            {/* Desktop table */}
            <div className="hidden overflow-x-auto rounded-xl border border-slate-800/60 lg:block">
                <table className="w-full min-w-[68rem] border-collapse text-left text-xs">
                    <thead className="bg-slate-950/60 text-slate-400">
                        <tr>
                            <th scope="col" className="px-4 py-3 text-[10px] font-semibold uppercase tracking-wider">Deployment</th>
                            <th scope="col" className="px-4 py-3 text-[10px] font-semibold uppercase tracking-wider">Model</th>
                            <th scope="col" className="px-4 py-3 text-[10px] font-semibold uppercase tracking-wider">Backend</th>
                            <th scope="col" className="px-4 py-3 text-[10px] font-semibold uppercase tracking-wider">Type</th>
                            <th scope="col" className="px-4 py-3 text-[10px] font-semibold uppercase tracking-wider">Location</th>
                            <th scope="col" className="px-4 py-3 text-[10px] font-semibold uppercase tracking-wider">Internal endpoint</th>
                            <th scope="col" className="px-4 py-3 text-right text-[10px] font-semibold uppercase tracking-wider">Actions</th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/70">
        {items.map((item) => {
                            const internalEndpoint = deploymentInternalEndpoint(item);
                            const busy = mutatingId === item.execution_id;
                            const connecting = connectingEndpointId === item.execution_id;
                            const expanded = expandedId === item.execution_id;
                            const pods = podsById[item.execution_id];
                            return (
                                <Fragment key={item.execution_id}>
                                <tr
                                    aria-busy={busy}
                                    className="align-top transition-colors hover:bg-slate-800/40"
                                >
                                    <td className="max-w-[260px] px-4 py-3 text-left">
                                        <div className="flex items-start gap-2">
                                            <div className="mt-0.5 flex w-8 shrink-0 items-center gap-1">
                                                {!item.pending && (
                                                    <ExpandToggle expanded={expanded} onToggle={() => toggleExpand(item.execution_id)} name={deploymentTitle(item)} />
                                                )}
                                                <StatusDot item={item} pods={pods} />
                                            </div>
                                            <div className="min-w-0 text-left">
                                                <span className="block truncate text-sm font-semibold text-slate-100" title={deploymentTitle(item)}>
                                                    {deploymentTitle(item)}
                                                </span>
                                                <div className="mt-0.5 line-clamp-2 text-left text-[11px] text-slate-500" title={item.description || ''}>
                                                    {item.description || 'No description'}
                                                </div>
                                            </div>
                                        </div>
                                    </td>
                                    <td className="max-w-[180px] truncate px-4 py-3 text-slate-300" title={item.model || ''}>{item.model || '—'}</td>
                                    <td className="px-4 py-3 text-slate-300">{item.backend || 'vLLM'}</td>
                                    <td className="px-4 py-3 text-slate-400">{deploymentKind(item)}</td>
                                    <td className="max-w-[200px] px-4 py-3">
                                        <div className="truncate text-slate-300" title={item.cluster_id || ''}>{clusterNameById[item.cluster_id] || item.cluster_id || '—'}</div>
                                        {item.namespace ? (
                                            <div className="flex items-center gap-1.5 mt-0.5">
                                                <span className="truncate text-[11px] font-mono text-slate-500" title={item.namespace}>{item.namespace}</span>
                                                <CopyButton value={item.namespace} copied={copiedEndpoint === item.namespace} onCopy={copyEndpoint} />
                                            </div>
                                        ) : (
                                            <div className="text-[11px] text-slate-500">—</div>
                                        )}
                                    </td>
                                    <td className="max-w-[220px] px-4 py-3">
                                        <EndpointCell value={internalEndpoint} copiedEndpoint={copiedEndpoint} onCopy={copyEndpoint} />
                                    </td>
                                    <td className="px-4 py-3">
                                        <RowActions item={item} busy={busy} connecting={connecting} onInspect={onInspect} onViewPodsLogs={onViewPodsLogs} onEdit={onEdit} onDelete={onDelete} onNavigate={onNavigate} onConnectLocalEndpoint={onConnectLocalEndpoint} onShare={onShare} />
                                    </td>
                                </tr>
                                {expanded && (
                                    <tr className="bg-slate-950/50">
                                        <td colSpan={7} className="border-t border-slate-800/60 p-0">
                                            <PodsExpandPanel
                                                pods={pods}
                                                loading={podsLoadingId === item.execution_id}
                                                error={podsErrorById[item.execution_id]}
                                            />
                                        </td>
                                    </tr>
                                )}
                                </Fragment>
                            );
                        })}
                    </tbody>
                </table>
            </div>

            {/* Narrow-screen cards */}
            <div className="flex flex-col gap-3 p-3 lg:hidden">
                {items.map((item) => {
                    const busy = mutatingId === item.execution_id;
                    const connecting = connectingEndpointId === item.execution_id;
                    const internalEndpoint = deploymentInternalEndpoint(item);
                    const expanded = expandedId === item.execution_id;
                    const pods = podsById[item.execution_id];
                    return (
                        <div
                            key={item.execution_id}
                            aria-busy={busy}
                            className="rounded-xl border border-slate-800/60 bg-slate-950/40 p-3"
                        >
                            <div className="flex items-start justify-between gap-2">
                                <div className="flex min-w-0 flex-1 items-start gap-2">
                                    <div className="mt-0.5 flex w-8 shrink-0 items-center gap-1">
                                        {!item.pending && (
                                            <ExpandToggle expanded={expanded} onToggle={() => toggleExpand(item.execution_id)} name={deploymentTitle(item)} />
                                        )}
                                        <StatusDot item={item} pods={pods} />
                                    </div>
                                    <div className="min-w-0 text-left">
                                        <span className="block truncate text-sm font-semibold text-slate-100">{deploymentTitle(item)}</span>
                                        <div className="mt-0.5 line-clamp-2 text-left text-[11px] text-slate-500">{item.description || 'No description'}</div>
                                    </div>
                                </div>
                            </div>
                            <dl className="mt-3 grid grid-cols-2 gap-2 text-[11px]">
                                <div><dt className="text-slate-600">Model</dt><dd className="truncate text-slate-300">{item.model || '—'}</dd></div>
                                <div><dt className="text-slate-600">Backend</dt><dd className="truncate text-slate-300">{item.backend || 'vLLM'}</dd></div>
                                <div><dt className="text-slate-600">Type</dt><dd className="truncate text-slate-300">{deploymentKind(item)}</dd></div>
                                <div><dt className="text-slate-600">Cluster</dt><dd className="truncate text-slate-300">{clusterNameById[item.cluster_id] || item.cluster_id || '—'}</dd></div>
                                <div>
                                    <dt className="text-slate-600">Namespace</dt>
                                    <dd className="flex items-center gap-1.5 truncate text-slate-300">
                                        <span className="truncate font-mono">{item.namespace || '—'}</span>
                                        {item.namespace && <CopyButton value={item.namespace} copied={copiedEndpoint === item.namespace} onCopy={copyEndpoint} />}
                                    </dd>
                                </div>
                            </dl>
                            {internalEndpoint && (
                                <div className="mt-3 grid gap-2 text-[11px]">
                                    <div>
                                        <div className="mb-1 text-slate-600">Internal endpoint</div>
                                        <EndpointCell value={internalEndpoint} copiedEndpoint={copiedEndpoint} onCopy={copyEndpoint} />
                                    </div>
                                </div>
                            )}
                            <div className="mt-3 border-t border-slate-800/60 pt-2">
                                <RowActions item={item} busy={busy} connecting={connecting} onInspect={onInspect} onViewPodsLogs={onViewPodsLogs} onEdit={onEdit} onDelete={onDelete} onNavigate={onNavigate} onConnectLocalEndpoint={onConnectLocalEndpoint} onShare={onShare} />
                            </div>
                            {expanded && (
                                <div className="mt-3 rounded-lg border border-slate-800/60 bg-slate-950/60">
                                    <PodsExpandPanel
                                        pods={pods}
                                        loading={podsLoadingId === item.execution_id}
                                        error={podsErrorById[item.execution_id]}
                                    />
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </>
    );
}

export default DeploymentTable;
