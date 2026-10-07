import { formatTimestamp } from '../../utils/formatTimestamp';
import { Info, Loader2, RefreshCw } from 'lucide-react';
import {
    capacityLabel,
    effectiveStorageStatus,
    hasNodeDrift,
    isCapacityDeclaredOnly,
    kindLabel,
    nodeDriftMessage,
    purposeLabel,
    resourcePhaseDotClass,
    shortId,
    statusDotClass,
    statusLabel,
    volumeLocation,
    volumeTitle,
} from './storagePresentation';

// Right-hand detail panels for the currently selected Storage volume's live
// PVC and PV objects. Kept as a separate component (rather than nested table
// rows) so the Storage volumes table can stay a plain list and the detail
// view has room to show every field without truncation.

function PhaseDot({ phase }) {
    const label = phase || 'Unknown';
    return (
        <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s] ${resourcePhaseDotClass(label)}`}
            title={label}
            aria-label={label}
        />
    );
}



function ResourceFields({ resource }) {
    if (!resource) {
        return <p className="text-xs text-slate-600">Not found</p>;
    }
    const createdAt = formatTimestamp(resource.createdAt, { invalid: (value) => value });
    return (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
            <div className="col-span-2">
                <dt className="text-slate-600">Name</dt>
                <dd className="mt-0.5 truncate font-mono text-slate-200" title={resource.name}>{resource.name}</dd>
            </div>
            {resource.namespace && (
                <div className="col-span-2">
                    <dt className="text-slate-600">Namespace</dt>
                    <dd className="mt-0.5 truncate font-mono text-slate-300">{resource.namespace}</dd>
                </div>
            )}
            <div>
                <dt className="text-slate-600">Capacity</dt>
                <dd className="mt-0.5 text-slate-300">{resource.capacity || '—'}</dd>
            </div>
            <div>
                <dt className="text-slate-600">Access modes</dt>
                <dd className="mt-0.5 text-slate-300">{(resource.accessModes || []).join(', ') || '—'}</dd>
            </div>
            <div>
                <dt className="text-slate-600">Volume mode</dt>
                <dd className="mt-0.5 text-slate-300">{resource.volumeMode || '—'}</dd>
            </div>
            <div>
                <dt className="text-slate-600">Storage class</dt>
                <dd className="mt-0.5 truncate text-slate-300">{resource.storageClassName || '—'}</dd>
            </div>
            {resource.reclaimPolicy && (
                <div>
                    <dt className="text-slate-600">Reclaim policy</dt>
                    <dd className="mt-0.5 text-slate-300">{resource.reclaimPolicy}</dd>
                </div>
            )}
            {resource.source && (
                <div className="col-span-2">
                    <dt className="text-slate-600">Source</dt>
                    <dd className="mt-0.5 truncate font-mono text-slate-300" title={resource.source.detail}>
                        {resource.source.type}: {resource.source.detail || '—'}
                    </dd>
                </div>
            )}
            {resource.volumeName && (
                <div className="col-span-2">
                    <dt className="text-slate-600">Bound volume</dt>
                    <dd className="mt-0.5 truncate font-mono text-slate-300" title={resource.volumeName}>{resource.volumeName}</dd>
                </div>
            )}
            {createdAt && (
                <div className="col-span-2">
                    <dt className="text-slate-600">Created</dt>
                    <dd className="mt-0.5 text-slate-300">{createdAt}</dd>
                </div>
            )}
            {resource.uid && (
                <div className="col-span-2">
                    <dt className="text-slate-600">UID</dt>
                    <dd className="mt-0.5 truncate font-mono text-slate-500" title={resource.uid}>{resource.uid}</dd>
                </div>
            )}
        </dl>
    );
}

function StatusDot({ status, title }) {
    const label = title || statusLabel(status);
    return (
        <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s] ${statusDotClass(status)}`}
            title={label}
            aria-label={label}
        />
    );
}

function PurposeBadges({ purposes }) {
    if (!Array.isArray(purposes) || purposes.length === 0) return null;
    return (
        <div className="flex flex-wrap gap-1">
            {purposes.map((purpose) => (
                <span
                    key={purpose}
                    className="inline-flex items-center rounded-full border border-sky-400/30 bg-sky-400/10 px-1.5 py-0.5 text-[10px] text-sky-200"
                >
                    {purposeLabel(purpose)}
                </span>
            ))}
        </div>
    );
}

// Storage volume metadata (name, id, cluster, location, capacity, status)
// shown above the PVC/PV panels so the two live-status blocks below it
// don't have to repeat the same information.
function VolumeSummary({ item, pvc, pv, clusterNameById, mutatingId, onAcknowledgeNodes }) {
    const effectiveStatus = effectiveStorageStatus(item, pvc, pv);
    const statusTitle = effectiveStatus !== item.status
        ? `${statusLabel(item.status)} (PVC: ${pvc ? pvc.phase || 'Unknown' : 'not found'}, PV: ${pv ? pv.phase || 'Unknown' : 'not found'})`
        : undefined;
    const drift = hasNodeDrift(item);
    const acknowledging = mutatingId === item.id;
    return (
        <div className="rounded-xl border border-slate-800/60 bg-slate-950/40 p-4">
            <div className="flex flex-wrap items-center gap-2">
                <StatusDot status={effectiveStatus} title={statusTitle} />
                <h2 className="truncate text-lg font-semibold text-slate-100" title={volumeTitle(item)}>
                    {volumeTitle(item)}
                </h2>
                <span className="text-[11px] text-slate-500">{statusTitle ? statusLabel(effectiveStatus) : statusLabel(item.status)}</span>
                <PurposeBadges purposes={item.purposes} />
            </div>
            {item.status === 'failed' && item.failureDetail && (
                <p className="mt-1 text-[11px] text-rose-300">{item.failureDetail}</p>
            )}
            {drift && (
                <div className="mt-3 flex flex-wrap items-start gap-2 rounded-lg border border-orange-400/30 bg-orange-400/10 p-2.5 text-[11px] text-orange-200">
                    <Info size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                    <p className="min-w-0 flex-1">{nodeDriftMessage(item)}</p>
                    <button
                        type="button"
                        onClick={() => onAcknowledgeNodes?.(item)}
                        disabled={acknowledging}
                        className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-orange-400/40 px-2 py-1 text-[11px] font-semibold text-orange-100 transition hover:bg-orange-400/20 disabled:opacity-50"
                    >
                        {acknowledging ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                        Acknowledge
                    </button>
                </div>
            )}
            <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-4">
                <div>
                    <dt className="text-slate-600">ID</dt>
                    <dd className="mt-0.5 truncate font-mono text-slate-300">#{shortId(item.id)}</dd>
                </div>
                <div>
                    <dt className="text-slate-600">Type</dt>
                    <dd className="mt-0.5 text-slate-300">{kindLabel(item.kind)}</dd>
                </div>
                <div>
                    <dt className="text-slate-600">Cluster</dt>
                    <dd className="mt-0.5 truncate text-slate-300" title={item.clusterId || ''}>
                        {clusterNameById[item.clusterId] || item.clusterId || '—'}
                    </dd>
                </div>
                <div>
                    <dt className="text-slate-600">Capacity</dt>
                    <dd className="mt-0.5 flex items-center gap-1 text-slate-300">
                        {capacityLabel(item)}
                        {isCapacityDeclaredOnly(item) && (
                            <span className="inline-flex shrink-0 items-center" title="Declared capacity, not enforced by the backend">
                                <Info size={11} className="text-slate-500" aria-hidden="true" />
                            </span>
                        )}
                    </dd>
                </div>
                <div className="col-span-2 sm:col-span-4">
                    <dt className="text-slate-600">Location</dt>
                    <dd className="mt-0.5 truncate font-mono text-slate-300" title={volumeLocation(item)}>
                        {volumeLocation(item)}
                    </dd>
                </div>
            </dl>
        </div>
    );
}

function ResourcePanel({ title, resource }) {
    const phase = resource?.phase;
    return (
        <div className="flex h-full flex-col rounded-xl border border-slate-800/60 bg-slate-950/40 p-4">
            <div className="flex items-center gap-2">
                <PhaseDot phase={phase} />
                <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-300">{title}</h3>
                {resource?.phase && (
                    <span className="ml-auto text-[11px] text-slate-500">{resource.phase}</span>
                )}
            </div>
            <div className="mt-3 flex-1">
                <ResourceFields resource={resource} />
            </div>
        </div>
    );
}

export function StorageResourceDetailPanel({ item, resourceStatuses = {}, clusterNameById = {}, mutatingId, onAcknowledgeNodes }) {
    if (!item) {
        return (
            <div className="flex h-full min-h-[16rem] items-center justify-center rounded-xl border border-dashed border-slate-800/60 bg-slate-950/20 p-4 text-center text-xs text-slate-600">
                Select a storage volume to view its PVC and PV status.
            </div>
        );
    }
    const claims = (resourceStatuses[item.id] || {}).persistentVolumeClaims || [];
    const pvc = claims.length ? claims[0] : null;
    const pv = pvc ? pvc.persistentVolume : null;
    return (
        <div className="flex h-full flex-col gap-3">
            <VolumeSummary
                item={item}
                pvc={pvc}
                pv={pv}
                clusterNameById={clusterNameById}
                mutatingId={mutatingId}
                onAcknowledgeNodes={onAcknowledgeNodes}
            />
            <ResourcePanel title="Persistent Volume Claim" resource={pvc} />
            <ResourcePanel title="Persistent Volume" resource={pv} />
        </div>
    );
}

export default StorageResourceDetailPanel;
