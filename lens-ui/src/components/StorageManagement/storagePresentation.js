// Presentation mapping for storage volume records returned by
// GET /api/v1/storage/volumes. Kept free of JSX and network code so the same
// rules can be reused by the table, the create wizard and the stat cards.

export const STORAGE_KIND_OPTIONS = ['local-disk', 'nfs', 'dynamic-pvc'];
export const STORAGE_STATUS_OPTIONS = ['pending', 'ready', 'failed', 'deleting'];

// Selectable storage purposes. Only "model-cache" is implemented today; the
// list is designed to grow (see llm_d_bench/storage/contracts.py
// StorageVolumePurpose) without needing UI rework.
export const PURPOSE_OPTIONS = ['model-cache'];

const PURPOSE_LABELS = {
    'model-cache': 'Model cache',
};

export function purposeLabel(purpose) {
    return PURPOSE_LABELS[purpose] || purpose || '—';
}

const KIND_LABELS = {
    'local-disk': 'hostPath',
    nfs: 'NFS',
    'dynamic-pvc': 'Dynamic PVC',
};

const STATUS_TONES = {
    ready: 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200',
    pending: 'border-amber-400/30 bg-amber-400/10 text-amber-200',
    failed: 'border-rose-400/30 bg-rose-400/10 text-rose-200',
    deleting: 'border-slate-400/30 bg-slate-400/10 text-slate-300',
    // Pseudo-status used only for display: a "ready" hostPath volume where
    // new cluster nodes were detected without the volume's content (see
    // `hasNodeDrift`/`effectiveStorageStatus`).
    attention: 'border-orange-400/30 bg-orange-400/10 text-orange-200',
};

// Pulsing status-dot colors, matching the dot used on the Deployments page.
const STATUS_DOT_CLASSES = {
    ready: 'bg-emerald-400',
    pending: 'bg-amber-400',
    failed: 'bg-red-400',
    deleting: 'bg-slate-500',
    attention: 'bg-orange-400',
};

// Kubernetes PVC/PV phases (Bound, Pending, Available, Released, Lost,
// Unknown) mapped to the same pulsing-dot palette used elsewhere, so the
// Storage table's resource rows read consistently with the rest of the app
// instead of introducing a separate pill/badge style.
const RESOURCE_PHASE_DOT_CLASSES = {
    Bound: 'bg-emerald-400',
    Available: 'bg-emerald-400',
    Pending: 'bg-amber-400',
    Released: 'bg-amber-400',
    Lost: 'bg-red-400',
    Unknown: 'bg-slate-500',
};

export function resourcePhaseDotClass(phase) {
    return RESOURCE_PHASE_DOT_CLASSES[phase] || RESOURCE_PHASE_DOT_CLASSES.Unknown;
}

// Maps a K8s PVC/PV phase onto the same vocabulary used for a Storage
// volume's own status (ready/pending/failed), so the two can be combined
// into a single displayed status below.
function resourcePhaseHealth(phase) {
    if (phase === 'Bound') return 'ready';
    if (phase === 'Pending' || phase === 'Available') return 'pending';
    // Released, Lost, Unknown, or missing entirely.
    return 'failed';
}

// The volume's own `status` field (pending/ready/failed/deleting) only
// reflects whether the backend *finished creating* the PV/PVC objects — it
// is never updated afterwards. If those objects later become unhealthy in
// Kubernetes (PV released/lost, PVC unbound, ...) a volume can still say
// "ready" while actually broken. This combines both so the status dot shown
// to users reflects the live state, not just the one-time creation result.
export function effectiveStorageStatus(item, pvc, pv) {
    if (!item) return 'deleting';
    if (item.status !== 'ready') return item.status;
    if (!pvc) return 'failed';
    const pvcHealth = resourcePhaseHealth(pvc.phase);
    if (pvcHealth !== 'ready') return pvcHealth;
    if (!pv) return 'failed';
    const pvHealth = resourcePhaseHealth(pv.phase);
    if (pvHealth !== 'ready') return pvHealth;
    if (hasNodeDrift(item)) return 'attention';
    return 'ready';
}

// True when a `local-disk` (hostPath) volume's baseline node set
// (`known_nodes`, captured at ready-time) is missing one or more cluster
// nodes that exist now — i.e. new nodes joined the cluster after this
// volume's content was provisioned, and the backend has not yet been told
// (via `acknowledge-nodes`) that they were backfilled. See
// `compute_node_drift` in llm_d_bench/storage/service.py.
export function hasNodeDrift(item) {
    return Array.isArray(item?.nodesAdded) && item.nodesAdded.length > 0;
}

export function nodeDriftMessage(item) {
    const nodes = item?.nodesAdded || [];
    if (!nodes.length) return '';
    const list = nodes.join(', ');
    return `${nodes.length} new node${nodes.length === 1 ? '' : 's'} joined the cluster after this hostPath volume `
        + `was provisioned and may not have its data yet: ${list}. Replicate the content onto ${nodes.length === 1 ? 'it' : 'them'}, `
        + 'then acknowledge to clear this warning.';
}

export function kindLabel(kind) {
    return KIND_LABELS[kind] || kind || '—';
}

export function statusLabel(status) {
    if (!status) return 'UNKNOWN';
    return String(status).toUpperCase();
}

export function statusTone(status) {
    return STATUS_TONES[status] || STATUS_TONES.deleting;
}

export function statusDotClass(status) {
    return STATUS_DOT_CLASSES[status] || STATUS_DOT_CLASSES.deleting;
}

export function volumeTitle(item) {
    return item?.name || item?.id || 'Storage volume';
}

export function shortId(id) {
    return String(id || '').slice(-8);
}

// Where a volume physically lives, for the table's location column.
export function volumeLocation(item) {
    if (!item) return '—';
    if (item.kind === 'local-disk' && item.localDisk) {
        return item.localDisk.hostPath;
    }
    if (item.kind === 'nfs' && item.nfs) {
        return `${item.nfs.server}:${item.nfs.path}`;
    }
    if (item.kind === 'dynamic-pvc' && item.dynamicPvc) {
        return `${item.dynamicPvc.storageClass} (${item.dynamicPvc.namespace})`;
    }
    return '—';
}

// Capacity is a declared value, not an enforced quota for local-disk/nfs (see
// docs/fern/pages/api-reference/storage.mdx).
export function capacityLabel(item) {
    if (!item?.capacity) return '—';
    return item.capacity;
}

// True when the capacity value is only a user-declared figure with no
// backend-enforced quota (currently just NFS). Used to render an info-icon
// tooltip next to the capacity value instead of an inline text suffix.
export function isCapacityDeclaredOnly(item) {
    return item?.kind === 'nfs' && Boolean(item?.capacity);
}

// Client-side guard mirroring the backend rule: in-use volumes cannot be
// deleted. The server stays authoritative.
export function deletionBlockedReason(item) {
    const inUseCount = Number(item?.inUseCount || 0);
    if (inUseCount > 0) {
        return `Cannot delete: used by ${inUseCount} deployment${inUseCount === 1 ? '' : 's'}`;
    }
    return '';
}

export function summarizeStatuses(items, resourceStatuses = {}) {
    const summary = { total: items.length, ready: 0, pending: 0, failed: 0, deleting: 0, attention: 0 };
    for (const item of items) {
        const claims = (resourceStatuses[item?.id] || {}).persistentVolumeClaims || [];
        const pvc = claims.length ? claims[0] : null;
        const pv = pvc ? pvc.persistentVolume : null;
        const status = effectiveStorageStatus(item, pvc, pv);
        if (status && Object.prototype.hasOwnProperty.call(summary, status)) {
            summary[status] += 1;
        } else if (status) {
            summary.failed += 1;
        }
    }
    return summary;
}

export function clusterOptions(items) {
    return [...new Set(items.map((item) => item?.clusterId).filter(Boolean))].sort();
}

export { errorMessage } from '../../utils/errorMessage';
