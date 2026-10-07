import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { useNotice } from '../../hooks/useNotice';
import { paginate } from '../../utils/pagination';
import { PaginationControls } from '../ui/PaginationControls';
import { useClusterNames } from '../OptimizationWorkspace/useClusterNames';
import { useResourceList } from '../../hooks/useResourceList';
import { AsyncState } from '../shared/AsyncState';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { usePolling } from '../../hooks/usePolling';
import { CheckCircle2, ChevronLeft, ChevronRight, Download, Plus, RefreshCw, Search, Server, ServerCog } from 'lucide-react';
import { createModelCacheEntry, deleteModelCacheEntry, listModelCacheEntries, retryModelCacheEntry, syncAllModelCacheEntryNodes, syncModelCacheEntryNodes } from './modelCacheBackend';
import { listStorageVolumes } from '../StorageManagement/storageManagementBackend';
import { EmptyState } from '../ui/EmptyState';
import { Button } from '../ui/Button';
import { Input, Select } from '../ui/FormControls';
import { SectionLabel } from '../ui/SectionLabel';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { ModelCacheCardGrid } from './ModelCacheCardGrid';
import { ModelCacheStorageList } from './ModelCacheStorageList';
import { DownloadModelModal } from './DownloadModelModal';
import { DeleteModelCacheModal } from './DeleteModelCacheModal';
import { ModelCacheLogsModal } from './ModelCacheLogsModal';
import { PermissionGate } from '../../features/auth/PermissionGate';
import { clusterOptions, errorMessage, hasPendingSync, sourceDisplayName, sourceRepository, summarizeStatuses } from './modelCachePresentation';
import { findModelByRepository } from '../../data/modelCatalog';

const SEARCH_DEBOUNCE_MS = 300;
const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;
const STORAGE_PAGE_SIZE = 6;
const POLL_INTERVAL_MS = 10000;

function StatCard({ label, value, tone = 'text-white' }) {
    return (
        <div className="flex h-full flex-col gap-2 rounded-xl border border-slate-700/60 bg-slate-800/60 p-4 text-left shadow-lg backdrop-blur-xl">
            <div className="truncate text-xs font-medium text-slate-400">{label}</div>
            <div className={`truncate text-3xl font-bold leading-none ${tone}`}>{value}</div>
        </div>
    );
}

export function ModelCachePage() {
    const [searchInput, setSearchInput] = useState('');
    const query = useDebouncedValue(searchInput, SEARCH_DEBOUNCE_MS).trim();
    const [typeFilter, setTypeFilter] = useState('');
    const [clusterId, setClusterId] = useState('');
    const [storageVolumeId, setStorageVolumeId] = useState('');
    const [dialog, setDialog] = useState(null);
    const [selected, setSelected] = useState(null);
    const [mutatingId, setMutatingId] = useState(null);
    const [syncingAll, setSyncingAll] = useState(false);
    const [notice, setNotice] = useNotice();
    const [page, setPage] = useState(0);
    const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

    const { clusters, clusterNameById } = useClusterNames();
    const [volumeNameById, setVolumeNameById] = useState({});

    // Storage volume names are looked up once so the table can show a
    // friendly name instead of a raw id; harmless if a volume is later
    // deleted (the id itself is still shown as a fallback).
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const volumes = await listStorageVolumes({});
                if (cancelled) return;
                const names = {};
                for (const volume of volumes) names[volume.id] = volume.name || volume.id;
                setVolumeNameById(names);
            } catch {
                // Non-fatal: the table falls back to showing the raw volume id.
            }
        })();
        return () => { cancelled = true; };
    }, []);

    // Storage volumes tagged as model-cache purpose, scoped to the selected
    // cluster (or all clusters when none is selected). Shown in the left
    // panel so users can see where cached models can go before downloading.
    const [modelCacheVolumes, setModelCacheVolumes] = useState([]);
    const [volumesLoading, setVolumesLoading] = useState(true);
    const [storagePage, setStoragePage] = useState(0);
    useEffect(() => {
        let cancelled = false;
        setVolumesLoading(true);
        setStorageVolumeId('');
        (async () => {
            try {
                const volumes = await listStorageVolumes({ clusterId, purpose: 'model-cache' });
                if (!cancelled) setModelCacheVolumes(volumes);
            } catch {
                if (!cancelled) setModelCacheVolumes([]);
            } finally {
                if (!cancelled) setVolumesLoading(false);
            }
        })();
        return () => { cancelled = true; };
    }, [clusterId]);

    useEffect(() => {
        setStoragePage(0);
    }, [clusterId, modelCacheVolumes.length]);

    const storageTotalPages = Math.max(1, Math.ceil(modelCacheVolumes.length / STORAGE_PAGE_SIZE));
    const storageCurrentPage = Math.min(storagePage, storageTotalPages - 1);
    const pagedModelCacheVolumes = useMemo(
        () => modelCacheVolumes.slice(storageCurrentPage * STORAGE_PAGE_SIZE, storageCurrentPage * STORAGE_PAGE_SIZE + STORAGE_PAGE_SIZE),
        [modelCacheVolumes, storageCurrentPage]
    );


    const fetchItems = useCallback(({ signal }) => listModelCacheEntries({ clusterId, signal }), [clusterId]);
    const { items, setItems, loading, refreshing, error, setError, load } = useResourceList(fetchItems, {
        errorFallback: 'Failed to load model cache entries',
    });



    // Only poll while an entry is still downloading/deleting; otherwise
    // nothing would change without a user action.
    const hasInFlight = items.some((item) => item.status === 'pending' || item.status === 'downloading' || item.status === 'deleting');
    usePolling(() => load({ quiet: true }), { intervalMs: POLL_INTERVAL_MS, enabled: hasInFlight });


    const UNLABELED_TYPE = 'Unlabeled';

    // Cross-reference each cached model's source repository against the
    // (frontend-only) Model Market catalog so we can show/filter by model
    // type -- there is no backend endpoint for this, the catalog is static.
    // Entries with no catalog match are labeled UNLABELED_TYPE so they can
    // still be filtered/grouped instead of being silently excluded.
    const modelTypeById = useMemo(() => {
        const map = {};
        for (const item of items) {
            const catalogModel = findModelByRepository(sourceRepository(item));
            map[item.id] = catalogModel ? catalogModel.category : UNLABELED_TYPE;
        }
        return map;
    }, [items]);

    const typeOptions = useMemo(() => {
        const options = [...new Set(Object.values(modelTypeById))].filter((type) => type !== UNLABELED_TYPE).sort();
        if (Object.values(modelTypeById).includes(UNLABELED_TYPE)) options.push(UNLABELED_TYPE);
        return options;
    }, [modelTypeById]);

    const filteredItems = useMemo(() => {
        const needle = query.trim().toLowerCase();
        return items.filter((item) => {
            if (typeFilter && modelTypeById[item.id] !== typeFilter) return false;
            if (storageVolumeId && item.storageVolumeId !== storageVolumeId) return false;
            if (needle && !sourceDisplayName(item).toLowerCase().includes(needle)) return false;
            return true;
        });
    }, [items, query, typeFilter, storageVolumeId, modelTypeById]);

    const summary = useMemo(() => summarizeStatuses(items), [items]);
    const pendingSyncCount = useMemo(() => items.filter(hasPendingSync).length, [items]);
    const mergedClusters = useMemo(() => {
        const merged = new Set([...clusters.map((cluster) => cluster.id), ...clusterOptions(items)]);
        if (clusterId) merged.add(clusterId);
        return [...merged].sort();
    }, [clusters, items, clusterId]);

    const hasFilters = Boolean(query || typeFilter || clusterId || storageVolumeId);

    useEffect(() => {
        setPage(0);
    }, [query, typeFilter, clusterId, storageVolumeId, items.length]);

    const { totalPages, currentPage, pageStart, pageEnd, pagedItems } = useMemo(
        () => paginate(filteredItems, page, pageSize),
        [filteredItems, page, pageSize]
    );

    const closeDialog = () => {
        setDialog(null);
        setSelected(null);
    };

    const openDelete = (item) => {
        setSelected(item);
        setDialog('delete');
    };

    const openLogs = (item) => {
        setSelected(item);
        setDialog('logs');
    };

    const handleCreate = async (payload) => {
        const created = await createModelCacheEntry(payload);
        setItems((current) => {
            const withoutDuplicate = current.filter((item) => item.id !== created.id);
            return [created, ...withoutDuplicate];
        });
        setNotice('Model download started');
        closeDialog();
    };

    const handleRetry = async (item) => {
        setMutatingId(item.id);
        try {
            const updated = await retryModelCacheEntry(item.id);
            setItems((current) => current.map((existing) => (existing.id === updated.id ? updated : existing)));
            setNotice('Retry started');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to retry download'));
        } finally {
            setMutatingId(null);
        }
    };

    const handleSyncNodes = async (item) => {
        setMutatingId(item.id);
        try {
            const updated = await syncModelCacheEntryNodes(item.id);
            setItems((current) => current.map((existing) => (existing.id === updated.id ? updated : existing)));
            setNotice('Sync started for new nodes');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to sync new nodes'));
        } finally {
            setMutatingId(null);
        }
    };

    const handleSyncAllNodes = async () => {
        setSyncingAll(true);
        try {
            const changed = await syncAllModelCacheEntryNodes({ clusterId: clusterId || undefined });
            if (changed.length) {
                setItems((current) => {
                    const byId = new Map(changed.map((entry) => [entry.id, entry]));
                    return current.map((existing) => byId.get(existing.id) || existing);
                });
                setNotice(`Sync started for ${changed.length} model${changed.length === 1 ? '' : 's'}`);
            } else {
                setNotice('Nothing to sync — all models are up to date');
            }
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to sync new nodes'));
        } finally {
            setSyncingAll(false);
        }
    };

    const handleDelete = async () => {
        const entryId = selected.id;
        setMutatingId(entryId);
        try {
            const updated = await deleteModelCacheEntry(entryId);
            if (updated.status === 'deleted') {
                // Removed synchronously on the backend (it never reached
                // "ready", so there was nothing on the shared volume that
                // needed a mount-dependent cleanup Job) -- drop it from the
                // list immediately instead of merging in the minimal payload.
                setItems((current) => current.filter((item) => item.id !== entryId));
                setNotice('Deleted');
            } else {
                setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)));
                setNotice('Deletion started');
            }
            closeDialog();
        } finally {
            setMutatingId(null);
        }
    };

    return (
        <ModulePage contentClassName="flex flex-col gap-4">
            <ModuleHeader
                icon={Download}
                title="Model cache"
                description="Download, monitor, and reuse cached model files on registered storage volumes before deployment."
                actions={(
                    <>
                        {notice && (
                            <span role="status" className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-2.5 py-1.5 text-[11px] text-emerald-200">
                                <CheckCircle2 size={13} aria-hidden="true" />
                                {notice}
                            </span>
                        )}
                        <Button variant="secondary" size="icon" onClick={load} disabled={refreshing} title="Refresh" aria-label="Refresh model cache entries">
                            <RefreshCw size={16} className={refreshing ? 'animate-spin' : ''} aria-hidden="true" />
                        </Button>
                        {pendingSyncCount > 0 && (
                            <PermissionGate permission="model-cache:entry:sync">
                                <Button
                                    variant="secondary"
                                    size="sm"
                                    onClick={handleSyncAllNodes}
                                    disabled={syncingAll}
                                    title="Download this model onto every new cluster node that doesn't have it yet"
                                    className="border-orange-400/40 text-orange-200 hover:bg-orange-400/10"
                                >
                                    <RefreshCw size={14} className={syncingAll ? 'animate-spin' : ''} aria-hidden="true" />
                                    Sync new nodes ({pendingSyncCount})
                                </Button>
                            </PermissionGate>
                        )}
                        <PermissionGate permission="model-cache:entry:create">
                            <Button variant="sky" size="sm" onClick={() => setDialog('download')}>
                                <Plus size={14} aria-hidden="true" /> Download model
                            </Button>
                        </PermissionGate>
                    </>
                )}
            />

            <section className="flex flex-col gap-3">
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                    <StatCard label="Total" value={summary.total} />
                    <StatCard label="Ready" value={summary.ready} />
                    <StatCard label="Downloading" value={summary.pending + summary.downloading} />
                    <StatCard label="Failed" value={summary.failed} tone={summary.failed > 0 ? 'text-rose-300' : 'text-white'} />
                    <StatCard label="Deleting" value={summary.deleting} />
                </div>
            </section>

            <div className="flex flex-col gap-4 lg:flex-row lg:items-stretch lg:h-[calc(100vh-17rem)] lg:min-h-[28rem]">
              <div className="flex flex-col gap-3 lg:h-full lg:w-72 lg:shrink-0">
                <div className="flex shrink-0 items-center gap-2.5 rounded-2xl border border-slate-800/80 bg-slate-900/50 p-1.5 pl-3 backdrop-blur-xl transition-colors hover:border-cyan-500/40">
                    <span className="inline-flex shrink-0 items-center gap-1.5 text-xs font-semibold tracking-wide text-cyan-400">
                        <Server size={14} />
                        <span className="hidden sm:inline">Target cluster</span>
                    </span>
                    <Select
                        id="model-cache-cluster-filter"
                        aria-label="Filter by cluster"
                        value={clusterId}
                        onChange={(event) => setClusterId(event.target.value)}
                        className="w-full min-w-0"
                    >
                        <option value="">All clusters</option>
                        {mergedClusters.map((cluster) => (
                            <option key={cluster} value={cluster}>{clusterNameById[cluster] || cluster}</option>
                        ))}
                    </Select>
                </div>
                <section className="flex min-h-0 flex-1 flex-col rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/90 via-slate-900/50 to-slate-950/90 p-4 shadow-2xl backdrop-blur-xl">
                    <SectionLabel className="shrink-0">Model cache storage</SectionLabel>
                    <div className="mt-3 min-h-0 flex-1 overflow-y-auto pr-1">
                        <ModelCacheStorageList
                            volumes={pagedModelCacheVolumes}
                            loading={volumesLoading}
                            clusterNameById={clusterNameById}
                            selectedVolumeId={storageVolumeId}
                            onSelectVolume={setStorageVolumeId}
                        />
                    </div>
                    {!volumesLoading && modelCacheVolumes.length > STORAGE_PAGE_SIZE && (
                        <div className="mt-2 flex shrink-0 items-center justify-center gap-2 border-t border-slate-800/60 pt-2 text-xs text-slate-400">
                            <Button
                                variant="secondary"
                                size="sm"
                                disabled={storageCurrentPage === 0}
                                onClick={() => setStoragePage((current) => Math.max(0, current - 1))}
                                aria-label="Previous storage page"
                            >
                                <ChevronLeft size={14} />
                            </Button>
                            <span className="min-w-16 text-center text-slate-300">{storageCurrentPage + 1} / {storageTotalPages}</span>
                            <Button
                                variant="secondary"
                                size="sm"
                                disabled={storageCurrentPage + 1 >= storageTotalPages}
                                onClick={() => setStoragePage((current) => Math.min(storageTotalPages - 1, current + 1))}
                                aria-label="Next storage page"
                            >
                                <ChevronRight size={14} />
                            </Button>
                        </div>
                    )}
                </section>
              </div>

              <section
                aria-busy={loading || refreshing}
                className="flex min-h-0 min-w-0 flex-1 flex-col rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/90 via-slate-900/50 to-slate-950/90 shadow-2xl backdrop-blur-xl"
              >
              <div className="flex min-h-0 flex-1 flex-col p-6">
                <SectionLabel className="shrink-0">{`Cached models (${filteredItems.length})`}</SectionLabel>
                <div className="mt-3 flex shrink-0 flex-col gap-3">
                    <div className="relative">
                        <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" aria-hidden="true" />
                        <Input
                            type="search"
                            value={searchInput}
                            onChange={(event) => setSearchInput(event.target.value)}
                            placeholder="Search by model"
                            aria-label="Search cached models"
                            className="h-10 py-2 pl-9 text-xs"
                        />
                    </div>
                    <div className="flex flex-wrap gap-2" role="tablist" aria-label="Filter by model type">
                        <button
                            type="button"
                            role="tab"
                            aria-selected={typeFilter === ''}
                            onClick={() => setTypeFilter('')}
                            className={`rounded-md border px-3 py-1.5 text-xs transition ${typeFilter === '' ? 'border-cyan-400 bg-cyan-400/10 text-cyan-200' : 'border-slate-700 text-slate-400 hover:border-slate-500'}`}
                        >
                            All
                        </button>
                        {typeOptions.map((option) => (
                            <button
                                key={option}
                                type="button"
                                role="tab"
                                aria-selected={typeFilter === option}
                                onClick={() => setTypeFilter(option)}
                                className={`rounded-md border px-3 py-1.5 text-xs transition ${typeFilter === option ? 'border-cyan-400 bg-cyan-400/10 text-cyan-200' : 'border-slate-700 text-slate-400 hover:border-slate-500'}`}
                            >
                                {option}
                            </button>
                        ))}
                    </div>
                </div>
                <div className="mt-4 min-h-0 flex-1 overflow-y-auto pr-1">
                <AsyncState loading={loading} error={error} empty={filteredItems.length === 0}
                    loadingContent={(
                    <p className="px-4 py-12 text-center text-xs text-slate-500">Loading model cache entries…</p>
                    )}
                    errorContent={(
                    <EmptyState
                        icon={<ServerCog size={28} />}
                        title="Could not load model cache entries"
                        message={error}
                        action={<Button variant="secondary" onClick={load}>Try again</Button>}
                    />
                    )}
                    emptyContent={(
                    <EmptyState
                        icon={<Download size={28} />}
                        title={hasFilters ? 'No cached models match your filters' : 'No cached models yet'}
                        message={
                            hasFilters
                                ? 'Adjust the search text, type or cluster filter to widen the results.'
                                : 'Download a model from HuggingFace into one of your storage volumes so Deployments can use it as a model cache.'
                        }
                        action={
                            hasFilters ? (
                                <Button
                                    variant="secondary"
                                    onClick={() => {
                                        setSearchInput('');
                                        setTypeFilter('');
                                        setClusterId('');
                                        setStorageVolumeId('');
                                    }}
                                >
                                    Clear filters
                                </Button>
                            ) : undefined
                        }
                    />
                    )}
                >

                    <>
                    <ModelCacheCardGrid
                        items={pagedItems}
                        mutatingId={mutatingId}
                        clusterNameById={clusterNameById}
                        modelTypeById={modelTypeById}
                        volumeNameById={volumeNameById}
                        onRetry={handleRetry}
                        onViewLogs={openLogs}
                        onDelete={openDelete}
                        onSyncNodes={handleSyncNodes}
                    />
                    <PaginationControls className="mt-4" page={currentPage} totalPages={totalPages} onPageChange={setPage}
                        total={filteredItems.length} pageStart={pageStart} pageEnd={pageEnd} itemLabel="cached models"
                        pageSize={pageSize} pageSizeOptions={PAGE_SIZE_OPTIONS} onPageSizeChange={setPageSize} pageSizeId="model-cache-page-size" />
                    </>
                </AsyncState>
                </div>
              </div>
            </section>
            </div>

            {dialog === 'download' && (
                <DownloadModelModal
                    clusters={clusters}
                    defaultClusterId={clusterId}
                    onCancel={closeDialog}
                    onCreate={handleCreate}
                />
            )}
            {dialog === 'delete' && <DeleteModelCacheModal entry={selected} onCancel={closeDialog} onDelete={handleDelete} />}
            {dialog === 'logs' && <ModelCacheLogsModal entry={selected} onClose={closeDialog} />}
        </ModulePage>
    );
}

export default ModelCachePage;
