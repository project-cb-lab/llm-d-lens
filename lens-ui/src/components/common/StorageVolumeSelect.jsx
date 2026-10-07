import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, Database, HardDrive, Network, Search, X } from 'lucide-react';
import { cn } from '../../utils/cn';
import { kindLabel, statusDotClass, statusLabel } from '../StorageManagement/storagePresentation';

const KIND_ICONS = {
    'local-disk': HardDrive,
    nfs: Network,
    'dynamic-pvc': Database,
};

function StorageKindIcon({ kind, className = 'text-slate-400' }) {
    const Icon = KIND_ICONS[kind] || HardDrive;
    return <Icon size={14} className={cn('shrink-0', className)} aria-hidden="true" />;
}

function StorageStatusDot({ status }) {
    return (
        <span
            className={cn('h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s]', statusDotClass(status))}
            title={statusLabel(status)}
            aria-label={statusLabel(status)}
        />
    );
}

// Only `ready` volumes actually have a usable PVC/PV -- others (pending,
// failed, deleting) are shown for visibility but cannot be picked.
function isVolumeSelectable(volume) {
    return volume?.status === 'ready';
}

export function StorageVolumeSelect({
    value,
    onChange,
    volumes,
    disabled = false,
    loading = false,
    placeholder = 'Select storage volume',
    emptyLabel = 'No storage volumes available',
    className = '',
    buttonClassName = '',
    optionDetail,
    clearable = false,
}) {
    const [open, setOpen] = useState(false);
    const [search, setSearch] = useState('');
    const rootRef = useRef(null);
    const selected = useMemo(() => volumes.find((volume) => volume.id === value) || null, [volumes, value]);
    const filteredVolumes = useMemo(() => {
        const needle = search.trim().toLowerCase();
        if (!needle) return volumes;
        return volumes.filter((volume) => {
            const detail = optionDetail ? optionDetail(volume) : kindLabel(volume.kind);
            return [
                volume.name,
                volume.id,
                volume.kind,
                kindLabel(volume.kind),
                detail,
            ].some((item) => String(item || '').toLowerCase().includes(needle));
        });
    }, [optionDetail, search, volumes]);

    useEffect(() => {
        const onPointerDown = (event) => {
            if (rootRef.current && !rootRef.current.contains(event.target)) {
                setSearch('');
                setOpen(false);
            }
        };
        const onKeyDown = (event) => {
            if (event.key === 'Escape') {
                setSearch('');
                setOpen(false);
            }
        };
        document.addEventListener('pointerdown', onPointerDown);
        document.addEventListener('keydown', onKeyDown);
        return () => {
            document.removeEventListener('pointerdown', onPointerDown);
            document.removeEventListener('keydown', onKeyDown);
        };
    }, []);

    const choose = (volume) => {
        if (!isVolumeSelectable(volume)) return;
        onChange(volume.id);
        setSearch('');
        setOpen(false);
    };

    const label = selected ? selected.name : (loading ? 'Loading storage volumes...' : placeholder);
    const isDisabled = disabled || loading || !volumes.length;

    return (
        <div ref={rootRef} className={cn('relative', className)}>
            <button
                type="button"
                disabled={isDisabled}
                onClick={() => {
                    if (open) setSearch('');
                    setOpen(!open);
                }}
                className={cn(
                    'flex h-10 w-full items-center justify-between gap-2 rounded-lg border border-slate-700 bg-slate-950 px-3 text-left text-sm text-slate-200 outline-none transition hover:border-slate-600 focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/10 disabled:cursor-not-allowed disabled:opacity-60',
                    buttonClassName,
                )}
            >
                <span className="flex min-w-0 items-center gap-2">
                    {selected && <StorageStatusDot status={selected.status} />}
                    {selected && <StorageKindIcon kind={selected.kind} />}
                    <span className={cn('truncate', !selected && 'text-slate-500')}>{volumes.length ? label : emptyLabel}</span>
                </span>
                <span className="flex shrink-0 items-center gap-1">
                    {clearable && selected && <span role="button" tabIndex={0} aria-label="Clear storage selection" onClick={(event) => { event.stopPropagation(); choose(''); }} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); choose(''); } }} className="rounded p-0.5 text-slate-500 hover:bg-slate-800 hover:text-slate-200"><X size={14} /></span>}
                    <ChevronDown size={15} className={cn('text-slate-500 transition', open && 'rotate-180')} aria-hidden="true" />
                </span>
            </button>
            {open && (
                <div className="absolute z-30 mt-1 w-full overflow-hidden rounded-lg border border-slate-700 bg-slate-950 shadow-xl">
                    <div className="border-b border-slate-800 p-2">
                        <div className="flex h-8 items-center gap-2 rounded-md border border-slate-700 bg-slate-900 px-2 text-slate-400">
                            <Search size={13} className="shrink-0" aria-hidden="true" />
                            <input
                                autoFocus
                                type="search"
                                value={search}
                                onChange={(event) => setSearch(event.target.value)}
                                placeholder="Search storage..."
                                className="h-full min-w-0 flex-1 bg-transparent text-xs text-slate-200 outline-none placeholder:text-slate-600"
                            />
                        </div>
                    </div>
                    <div className="max-h-56 overflow-auto py-1">
                        {filteredVolumes.map((volume) => {
                            const selectable = isVolumeSelectable(volume);
                            return (
                                <button
                                    key={volume.id}
                                    type="button"
                                    onClick={() => choose(volume)}
                                    disabled={!selectable}
                                    title={selectable ? undefined : `Not selectable: storage is ${statusLabel(volume.status).toLowerCase()}, not ready`}
                                    className={cn(
                                        'flex w-full items-start gap-2 px-3 py-2 text-left text-sm hover:bg-slate-800/80 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent',
                                        volume.id === value ? 'bg-cyan-500/10 text-cyan-100' : 'text-slate-200',
                                    )}
                                >
                                    <StorageStatusDot status={volume.status} />
                                    <StorageKindIcon kind={volume.kind} className={volume.id === value ? 'text-cyan-300' : 'text-slate-400'} />
                                    <span className="min-w-0 flex-1">
                                        <span className="min-w-0 truncate">{volume.name}</span>
                                        <span className="mt-0.5 block truncate text-[10px] text-slate-500">
                                            {optionDetail ? optionDetail(volume) : kindLabel(volume.kind)}
                                            {!selectable ? ` · ${statusLabel(volume.status)}` : ''}
                                        </span>
                                    </span>
                                </button>
                            );
                        })}
                        {!filteredVolumes.length && (
                            <div className="px-3 py-3 text-xs text-slate-500">No matching storage volumes</div>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
}
