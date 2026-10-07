import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { useNotice } from '../../hooks/useNotice';
import { paginate } from '../../utils/pagination';
import { PaginationControls } from '../ui/PaginationControls';
import { useClusterNames } from '../OptimizationWorkspace/useClusterNames';
import { useResourceList } from '../../hooks/useResourceList';
import { AsyncState } from '../shared/AsyncState';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { usePolling } from '../../hooks/usePolling';
import { CheckCircle2, HardDrive, Info, PanelLeftClose, PanelLeftOpen, Plus, RefreshCw, Search, ServerCog, Server } from 'lucide-react';
import {
    createStorageVolume,
    acknowledgeStorageVolumeNodes,
    deleteStorageVolume,
    getStorageResourceStatuses,
    listStorageVolumes,
} from './storageManagementBackend';
import { EmptyState } from '../ui/EmptyState';
import { Button } from '../ui/Button';
import { Input, Select } from '../ui/FormControls';
import { SectionLabel } from '../ui/SectionLabel';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { StorageVolumeTable } from './StorageVolumeTable';
import { StorageResourceDetailPanel } from './StorageResourceDetailPanel';
import { CreateStorageVolumeModal } from './CreateStorageVolumeModal';
import { DeleteStorageVolumeModal } from './DeleteStorageVolumeModal';
import { PermissionGate } from '../../features/auth/PermissionGate';
import { PURPOSE_OPTIONS, STORAGE_KIND_OPTIONS, clusterOptions, kindLabel, purposeLabel, summarizeStatuses } from './storagePresentation';

const SEARCH_DEBOUNCE_MS = 300;
const DEFAULT_PAGE_SIZE = 10;
const RESOURCE_STATUS_POLL_INTERVAL_MS = 5000;

function StatCard({ label, value, tone = 'text-white', hint }) {
    return (
        <div className="flex h-full flex-col gap-2 rounded-xl border border-slate-700/60 bg-slate-800/60 p-4 text-left shadow-lg backdrop-blur-xl">
            <div className="flex items-center gap-1 truncate text-xs font-medium text-slate-400">
                {label}
                {hint && (
                    <span className="inline-flex shrink-0 items-center" title={hint}>
                        <Info size={12} className="text-slate-500" aria-hidden="true" />
                        <span className="sr-only">{hint}</span>
                    </span>
                )}
            </div>
            <div className={`truncate text-3xl font-bold leading-none ${tone}`}>{value}</div>
        </div>
    );
}

export function StorageManagementPage() {
    const [resourceStatuses, setResourceStatuses] = useState({});
    const [selectedResourceId, setSelectedResourceId] = useState(null);
    const [searchInput, setSearchInput] = useState('');
    const query = useDebouncedValue(searchInput, SEARCH_DEBOUNCE_MS).trim();
    const [kind, setKind] = useState('');
    const [purpose, setPurpose] = useState('');
    const [clusterId, setClusterId] = useState('');
    const [dialog, setDialog] = useState(null);
    const [selected, setSelected] = useState(null);
    const [mutatingId, setMutatingId] = useState(null);
    const [notice, setNotice] = useNotice();
    const [page, setPage] = useState(0);
    const [sidebarOpen, setSidebarOpen] = useState(true);
    const pageSize = DEFAULT_PAGE_SIZE;

    const statusControllerRef = useRef(null);
    // Cluster choices come from the unfiltered list so narrowing by cluster
    // never removes the option the user just picked.
    const [knownClusters, setKnownClusters] = useState([]);
    const { clusters, clusterNameById } = useClusterNames();


    const fetchItems = useCallback(({ signal }) => listStorageVolumes({ query, kind, purpose, clusterId, signal }),
        [query, kind, purpose, clusterId]);
    const rememberClusters = useCallback(results => {
        if (!query && !kind && !clusterId) setKnownClusters(clusterOptions(results));
    }, [query, kind, clusterId]);
    const { items, setItems, loading, refreshing, error, load } = useResourceList(fetchItems, {
        onLoaded: rememberClusters, errorFallback: 'Failed to load storage volumes',
    });

    const hasInFlightVolume = items.some((item) => item.status === 'pending' || item.status === 'deleting');
    usePolling(() => load({ quiet: true }), { intervalMs: RESOURCE_STATUS_POLL_INTERVAL_MS, enabled: hasInFlightVolume });

    useEffect(() => {
        const volumeIds = items.map((item) => item.id);
        if (!volumeIds.length) {
            setResourceStatuses({});
            return undefined;
        }
        let cancelled = false;
        const refreshResourceStatuses = async () => {
            statusControllerRef.current?.abort();
            const controller = new AbortController();
            statusControllerRef.current = controller;
            try {
                const statuses = await getStorageResourceStatuses(volumeIds, { signal: controller.signal });
                if (!cancelled) setResourceStatuses(statuses);
            } catch (failure) {
                if (failure?.name !== 'AbortError') {
                    // Preserve the most recently observed Kubernetes states.
                }
            }
        };
        refreshResourceStatuses();
        const timer = setInterval(refreshResourceStatuses, RESOURCE_STATUS_POLL_INTERVAL_MS);
        return () => {
            cancelled = true;
            clearInterval(timer);
            statusControllerRef.current?.abort();
        };
    }, [items]);

    useEffect(() => () => {
        statusControllerRef.current?.abort();
    }, []);


    const summary = useMemo(() => summarizeStatuses(items, resourceStatuses), [items, resourceStatuses]);
    const mergedClusters = useMemo(() => {
        const merged = new Set([...knownClusters, ...clusterOptions(items)]);
        if (clusterId) merged.add(clusterId);
        return [...merged].sort();
    }, [knownClusters, items, clusterId]);

    const hasFilters = Boolean(query || kind || purpose || clusterId);

    useEffect(() => {
        setPage(0);
    }, [query, kind, purpose, clusterId, items.length]);

    const { totalPages, currentPage, pagedItems } = useMemo(
        () => paginate(items, page, pageSize),
        [items, page, pageSize]
    );

    // Keep the side detail panel pointed at a row on the current page: default
    // to the first row, and fall back to it whenever the previous selection
    // scrolls out of view (filter change, deletion, pagination, ...).
    useEffect(() => {
        if (pagedItems.length === 0) {
            if (selectedResourceId !== null) setSelectedResourceId(null);
            return;
        }
        if (!pagedItems.some((item) => item.id === selectedResourceId)) {
            setSelectedResourceId(pagedItems[0].id);
        }
    }, [pagedItems, selectedResourceId]);

    const selectedResourceItem = pagedItems.find((item) => item.id === selectedResourceId) || null;

    const closeDialog = () => {
        setDialog(null);
        setSelected(null);
    };

    const openDelete = (item) => {
        setSelected(item);
        setDialog('delete');
    };

    const handleCreate = async (payload) => {
        const created = await createStorageVolume(payload);
        setItems((current) => [created, ...current]);
        setNotice('Storage volume creation started');
        closeDialog();
    };

    const handleDelete = async (keepModelFiles) => {
        const volumeId = selected.id;
        setMutatingId(volumeId);
        try {
            const updated = await deleteStorageVolume(volumeId, { keepModelFiles });
            if (updated?.id) {
                setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)));
            } else {
                // Older backend still returning 204: the volume is already gone.
                setItems((current) => current.filter((item) => item.id !== volumeId));
            }
            setNotice('Storage volume deletion started');
            closeDialog();
        } finally {
            setMutatingId(null);
        }
    };

    const handleAcknowledgeNodes = async (item) => {
        setMutatingId(item.id);
        try {
            const updated = await acknowledgeStorageVolumeNodes(item.id);
            setItems((current) => current.map((entry) => (entry.id === updated.id ? updated : entry)));
            setNotice('New nodes acknowledged');
        } finally {
            setMutatingId(null);
        }
    };

    return (
        <ModulePage contentClassName="flex flex-col gap-4">
            <ModuleHeader
                icon={HardDrive}
                title="Storage"
                description="Manage registered storage volumes for model cache, datasets, and deployment artifacts across clusters."
                actions={(
                    <>
                        {notice && (
                            <span role="status" className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-2.5 py-1.5 text-[11px] text-emerald-200">
                                <CheckCircle2 size={13} aria-hidden="true" />
                                {notice}
                            </span>
                        )}
                        <Button variant="secondary" size="icon" onClick={load} disabled={refreshing} title="Refresh" aria-label="Refresh storage volumes">
                            <RefreshCw size={16} className={refreshing ? 'animate-spin' : ''} aria-hidden="true" />
                        </Button>
                        <PermissionGate permission="storage:volume:create">
                            <Button variant="sky" size="sm" onClick={() => setDialog('create')}>
                                <Plus size={14} aria-hidden="true" /> New storage volume
                            </Button>
                        </PermissionGate>
                    </>
                )}
            />

            <section className="flex flex-col gap-3">
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                    <StatCard label="Total" value={summary.total} />
                    <StatCard label="Ready" value={summary.ready} />
                    <StatCard
                        label="Pending"
                        value={summary.pending}
                        hint="Volume created; Kubernetes PV/PVC objects are still being provisioned in the background. Becomes Ready or Failed automatically once provisioning finishes."
                    />
                    <StatCard label="Attention" value={summary.attention} tone={summary.attention > 0 ? 'text-orange-300' : 'text-white'} />
                    <StatCard label="Failed" value={summary.failed} tone={summary.failed > 0 ? 'text-rose-300' : 'text-white'} />
                </div>
            </section>

            <div className="flex flex-col gap-4 lg:flex-row lg:items-stretch lg:h-[calc(100vh-17rem)] lg:min-h-[28rem]">
            <div className={`flex flex-col gap-3 transition-all duration-200 lg:h-full ${sidebarOpen ? 'lg:w-80 lg:shrink-0' : 'lg:w-auto'}`}>
                <div className="flex shrink-0 items-center gap-2">
                    {sidebarOpen && (
                        <div className="flex min-w-0 flex-1 items-center gap-2.5 rounded-2xl border border-slate-800/80 bg-slate-900/50 p-1.5 pl-3 backdrop-blur-xl transition-colors hover:border-cyan-500/40">
                            <span className="inline-flex shrink-0 items-center gap-1.5 text-xs font-semibold tracking-wide text-cyan-400">
                                <Server size={14} />
                                <span className="hidden sm:inline">Cluster</span>
                            </span>
                            <Select
                                id="storage-cluster-filter"
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
                    )}
                    <Button
                        variant="secondary"
                        size="icon"
                        onClick={() => setSidebarOpen((current) => !current)}
                        title={sidebarOpen ? 'Collapse storage list' : 'Expand storage list'}
                        aria-label={sidebarOpen ? 'Collapse storage list' : 'Expand storage list'}
                        className="hidden shrink-0 lg:inline-flex"
                    >
                        {sidebarOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
                    </Button>
                </div>
            {sidebarOpen && (
            <section
                aria-busy={loading || refreshing}
                className="relative flex min-h-0 flex-1 flex-col rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/90 via-slate-900/50 to-slate-950/90 shadow-2xl backdrop-blur-xl"
            >
              <div className="flex min-h-0 flex-1 flex-col p-4">
                <div className="mt-3 flex shrink-0 flex-col gap-2">
                    <div className="relative">
                        <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" aria-hidden="true" />
                        <Input
                            type="search"
                            value={searchInput}
                            onChange={(event) => setSearchInput(event.target.value)}
                            placeholder="Search by name"
                            aria-label="Search storage volumes"
                            className="h-10 py-2 pl-9 text-xs"
                        />
                    </div>
                    <select
                        value={kind}
                        onChange={(event) => setKind(event.target.value)}
                        aria-label="Filter by type"
                        className="h-10 w-full rounded-xl border border-slate-800/40 bg-[#0b0f17] px-3 text-xs text-slate-300 outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/10"
                    >
                        <option value="">All types</option>
                        {STORAGE_KIND_OPTIONS.map((option) => (
                            <option key={option} value={option}>{kindLabel(option)}</option>
                        ))}
                    </select>
                    <select
                        value={purpose}
                        onChange={(event) => setPurpose(event.target.value)}
                        aria-label="Filter by purpose"
                        className="h-10 w-full rounded-xl border border-slate-800/40 bg-[#0b0f17] px-3 text-xs text-slate-300 outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/10"
                    >
                        <option value="">All purposes</option>
                        {PURPOSE_OPTIONS.map((option) => (
                            <option key={option} value={option}>{purposeLabel(option)}</option>
                        ))}
                    </select>
                </div>
                <div className="mt-3 flex min-h-0 flex-1 flex-col">
                <div className="min-h-0 flex-1 overflow-y-auto pr-1">
                <AsyncState loading={loading} error={error} empty={items.length === 0}
                    loadingContent={(
                    <p className="px-4 py-12 text-center text-xs text-slate-500">Loading storage volumes…</p>
                    )}
                    errorContent={(
                    <EmptyState
                        icon={<ServerCog size={28} />}
                        title="Could not load storage volumes"
                        message={error}
                        action={<Button variant="secondary" onClick={load}>Try again</Button>}
                    />
                    )}
                    emptyContent={(
                    <EmptyState
                        icon={<HardDrive size={28} />}
                        title={hasFilters ? 'No storage volumes match your filters' : 'No storage volumes yet'}
                        message={
                            hasFilters
                                ? 'Adjust the search text, type or cluster filter to widen the results.'
                                : 'Register a hostPath, NFS share or dynamic PVC that Deployments can mount as a model cache.'
                        }
                        action={
                            hasFilters ? (
                                <Button
                                    variant="secondary"
                                    onClick={() => {
                                        setSearchInput('');
                                        setKind('');
                                        setClusterId('');
                                    }}
                                >
                                    Clear filters
                                </Button>
                            ) : undefined
                        }
                    />
                    )}
                >

                    <StorageVolumeTable
                        items={pagedItems}
                        resourceStatuses={resourceStatuses}
                        mutatingId={mutatingId}
                        clusterNameById={clusterNameById}
                        onDelete={openDelete}
                        selectedId={selectedResourceId}
                        onSelectRow={setSelectedResourceId}
                    />
                </AsyncState>
                </div>
                {!loading && !error && items.length > 0 && (
                    <div className="mt-2 flex shrink-0 flex-col gap-3 border-t border-slate-800/60 px-1 pt-4 text-xs text-slate-400 sm:flex-row sm:items-center sm:justify-end">
                        <div className="flex items-center gap-2">
                            <PaginationControls page={currentPage} totalPages={totalPages} onPageChange={setPage} />
                        </div>
                    </div>
                )}
                </div>
              </div>
            </section>
            )}
            </div>

            <section className="flex min-h-0 min-w-0 flex-1 flex-col rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/90 via-slate-900/50 to-slate-950/90 p-6 shadow-2xl backdrop-blur-xl">
                <SectionLabel className="shrink-0">Status</SectionLabel>
                <div className="mt-3 min-h-0 flex-1 overflow-y-auto pr-1">
                    <StorageResourceDetailPanel
                        item={selectedResourceItem}
                        resourceStatuses={resourceStatuses}
                        clusterNameById={clusterNameById}
                        mutatingId={mutatingId}
                        onAcknowledgeNodes={handleAcknowledgeNodes}
                    />
                </div>
            </section>
            </div>

            {dialog === 'create' && (
                <CreateStorageVolumeModal
                    clusters={clusters}
                    defaultClusterId={clusterId}
                    onCancel={closeDialog}
                    onCreate={handleCreate}
                />
            )}
            {dialog === 'delete' && <DeleteStorageVolumeModal volume={selected} onCancel={closeDialog} onDelete={handleDelete} />}
        </ModulePage>
    );
}

export default StorageManagementPage;
