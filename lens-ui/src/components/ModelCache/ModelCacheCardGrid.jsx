import { AlertTriangle, FileText, Loader2, RefreshCw, RotateCw, Trash2 } from 'lucide-react';
import { PermissionGate } from '../../features/auth/PermissionGate';
import {
    effectiveModelCacheStatus,
    hasPendingSync,
    nodeProgressSummary,
    pendingSyncMessage,
    shortId,
    sourceDisplayName,
    sourceKindLabel,
    statusDotClass,
    statusLabel,
} from './modelCachePresentation';

function StatusDot({ status }) {
    return (
        <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s] ${statusDotClass(status)}`}
            title={statusLabel(status)}
            aria-label={statusLabel(status)}
        />
    );
}

function ModelCacheCard({ item, modelType, busy, clusterNameById, volumeNameById, onRetry, onViewLogs, onDelete, onSyncNodes }) {
    const canRetry = item.status === 'failed';
    const canDelete = item.status !== 'deleting';
    const pendingSync = hasPendingSync(item);
    return (
        <div
            aria-busy={busy}
            className="group flex flex-col rounded-lg border border-slate-800 bg-slate-900/70 p-3 transition hover:border-cyan-400/60 hover:bg-slate-900"
        >
            <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                    <div className="flex items-center gap-1.5">
                        <StatusDot status={effectiveModelCacheStatus(item)} />
                        <h3 className="truncate text-xs font-semibold text-white" title={sourceDisplayName(item)}>
                            {sourceDisplayName(item)}
                        </h3>
                    </div>
                    <p className="mt-0.5 truncate text-[10px] text-slate-500">{sourceKindLabel(item.source?.kind)} · #{shortId(item.id)}</p>
                    {modelType ? (
                        <span className="mt-1 inline-flex items-center rounded-full border border-cyan-400/30 bg-cyan-400/10 px-1.5 py-0.5 text-[9px] font-medium text-cyan-200">
                            {modelType}
                        </span>
                    ) : (
                        <span className="mt-1 inline-flex items-center rounded-full border border-slate-600/40 bg-slate-700/20 px-1.5 py-0.5 text-[9px] font-medium text-slate-400">
                            Unlabeled
                        </span>
                    )}
                </div>
                {busy && <Loader2 size={13} className="mt-0.5 shrink-0 animate-spin text-cyan-300" aria-hidden="true" />}
            </div>
            <dl className="mt-2 space-y-1 text-[10px]">
                <div className="flex items-center justify-between gap-2">
                    <dt className="shrink-0 text-slate-600">Cluster</dt>
                    <dd className="truncate text-right text-slate-300" title={item.clusterId || ''}>
                        {clusterNameById[item.clusterId] || item.clusterId || '—'}
                    </dd>
                </div>
                <div className="flex items-center justify-between gap-2">
                    <dt className="shrink-0 text-slate-600">Storage</dt>
                    <dd className="truncate text-right text-slate-300" title={item.storageVolumeId || ''}>
                        {volumeNameById[item.storageVolumeId] || item.storageVolumeId || '—'}
                    </dd>
                </div>
                <div className="flex items-center justify-between gap-2">
                    <dt className="shrink-0 text-slate-600">Progress</dt>
                    <dd className="truncate text-right text-slate-300">{item.status === 'ready' ? 'Complete' : nodeProgressSummary(item)}</dd>
                </div>
            </dl>
            {item.status === 'failed' && item.failureDetail && (
                <div className="mt-2 flex items-start gap-1 text-[10px] text-rose-300" title={item.failureDetail}>
                    <AlertTriangle size={10} className="mt-0.5 shrink-0" aria-hidden="true" />
                    <span className="line-clamp-2">{item.failureDetail}</span>
                </div>
            )}
            {pendingSync && (
                <div className="mt-2 flex items-start gap-1 text-[10px] text-orange-300" title={pendingSyncMessage(item)}>
                    <AlertTriangle size={10} className="mt-0.5 shrink-0" aria-hidden="true" />
                    <span className="line-clamp-2">New node{item.pendingSyncNodes.length === 1 ? '' : 's'} need this model synced</span>
                </div>
            )}
            <div className="mt-2 flex items-center justify-end gap-0.5 border-t border-slate-800/60 pt-2">
                <button
                    type="button"
                    onClick={() => onViewLogs(item)}
                    disabled={busy}
                    aria-label={`View logs for ${sourceDisplayName(item)}`}
                    title="View logs"
                    className="rounded-lg p-1 text-slate-400 transition hover:bg-slate-700/50 hover:text-slate-200 disabled:opacity-40"
                >
                    <FileText size={12} />
                </button>
                {pendingSync && (
                    <PermissionGate permission="model-cache:entry:sync">
                        <button
                            type="button"
                            onClick={() => onSyncNodes(item)}
                            disabled={busy}
                            aria-label={`Sync new nodes for ${sourceDisplayName(item)}`}
                            title="Sync new nodes"
                            className="rounded-lg p-1 text-slate-400 transition hover:bg-orange-500/10 hover:text-orange-300 disabled:opacity-40"
                        >
                            <RefreshCw size={12} />
                        </button>
                    </PermissionGate>
                )}
                {canRetry && (
                    <PermissionGate permission="model-cache:entry:retry">
                        <button
                            type="button"
                            onClick={() => onRetry(item)}
                            disabled={busy}
                            aria-label={`Retry ${sourceDisplayName(item)}`}
                            title="Retry download"
                            className="rounded-lg p-1 text-slate-400 transition hover:bg-cyan-500/10 hover:text-cyan-300 disabled:opacity-40"
                        >
                            <RotateCw size={12} />
                        </button>
                    </PermissionGate>
                )}
                <PermissionGate permission="model-cache:entry:delete">
                    <button
                        type="button"
                        onClick={() => onDelete(item)}
                        disabled={busy || !canDelete}
                        aria-label={`Delete ${sourceDisplayName(item)}`}
                        title={canDelete ? 'Delete cached model' : 'Already deleting'}
                        className="rounded-lg p-1 text-slate-400 transition hover:bg-rose-500/10 hover:text-rose-300 disabled:opacity-40"
                    >
                        <Trash2 size={12} />
                    </button>
                </PermissionGate>
            </div>
        </div>
    );
}

export function ModelCacheCardGrid({ items, mutatingId, clusterNameById = {}, volumeNameById = {}, modelTypeById = {}, onRetry, onViewLogs, onDelete, onSyncNodes }) {
    return (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
            {items.map((item) => (
                <ModelCacheCard
                    key={item.id}
                    item={item}
                    modelType={modelTypeById[item.id]}
                    busy={mutatingId === item.id}
                    clusterNameById={clusterNameById}
                    volumeNameById={volumeNameById}
                    onRetry={onRetry}
                    onViewLogs={onViewLogs}
                    onDelete={onDelete}
                    onSyncNodes={onSyncNodes}
                />
            ))}
        </div>
    );
}

export default ModelCacheCardGrid;
