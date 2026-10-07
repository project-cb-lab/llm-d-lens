import { HardDrive } from 'lucide-react';
import { capacityLabel, statusDotClass, statusLabel, volumeLocation } from '../StorageManagement/storagePresentation';

function VolumeStatusDot({ status }) {
    return (
        <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s] ${statusDotClass(status)}`}
            title={statusLabel(status)}
            aria-label={statusLabel(status)}
        />
    );
}

export function ModelCacheStorageList({ volumes, loading, clusterNameById = {}, selectedVolumeId = '', onSelectVolume }) {
    if (loading) {
        return <p className="px-2 py-6 text-center text-[11px] text-slate-500">Loading storage…</p>;
    }
    if (!volumes.length) {
        return (
            <div className="rounded-xl border border-dashed border-slate-800/60 bg-slate-950/30 px-3 py-6 text-center text-[11px] text-slate-500">
                <HardDrive size={18} className="mx-auto mb-2 text-slate-600" aria-hidden="true" />
                No model cache storage for this selection.
            </div>
        );
    }
    return (
        <div className="flex flex-col gap-2">
            {volumes.map((volume) => {
                const isSelected = selectedVolumeId === volume.id;
                return (
                    <button
                        key={volume.id}
                        type="button"
                        aria-pressed={isSelected}
                        onClick={() => onSelectVolume?.(isSelected ? '' : volume.id)}
                        className={`w-full rounded-xl border p-3 text-left transition ${
                            isSelected
                                ? 'border-cyan-400/60 bg-cyan-400/10'
                                : 'border-slate-800/60 bg-slate-950/40 hover:border-slate-600'
                        }`}
                    >
                        <div className="flex items-center gap-1.5">
                            <VolumeStatusDot status={volume.status} />
                            <span className="truncate text-xs font-semibold text-slate-100" title={volume.name}>{volume.name}</span>
                        </div>
                        <dl className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
                            <div><dt className="text-slate-600">Capacity</dt><dd className="truncate text-slate-300">{capacityLabel(volume)}</dd></div>
                            <div><dt className="text-slate-600">Cluster</dt><dd className="truncate text-slate-300">{clusterNameById[volume.clusterId] || volume.clusterId || '—'}</dd></div>
                            <div className="col-span-2"><dt className="text-slate-600">Location</dt><dd className="truncate font-mono text-slate-300">{volumeLocation(volume)}</dd></div>
                        </dl>
                    </button>
                );
            })}
        </div>
    );
}

export default ModelCacheStorageList;
