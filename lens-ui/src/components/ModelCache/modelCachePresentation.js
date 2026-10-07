// Presentation mapping for model cache entries returned by
// GET /api/v1/model-cache/entries. Kept free of JSX and network code so the
// same rules can be reused by the table, the download modal and the stat cards.

export const MODEL_CACHE_STATUS_OPTIONS = ['pending', 'downloading', 'ready', 'failed', 'deleting'];

const STATUS_TONES = {
    ready: 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200',
    pending: 'border-amber-400/30 bg-amber-400/10 text-amber-200',
    downloading: 'border-cyan-400/30 bg-cyan-400/10 text-cyan-200',
    failed: 'border-rose-400/30 bg-rose-400/10 text-rose-200',
    deleting: 'border-slate-400/30 bg-slate-400/10 text-slate-300',
    // Pseudo-status used only for display: a "ready" entry on a hostPath
    // volume where new cluster nodes were detected without this model's
    // content yet (see `hasPendingSync`/`effectiveModelCacheStatus`).
    attention: 'border-orange-400/30 bg-orange-400/10 text-orange-200',
};

const STATUS_DOT_CLASSES = {
    ready: 'bg-emerald-400',
    pending: 'bg-amber-400',
    downloading: 'bg-cyan-400',
    failed: 'bg-red-400',
    deleting: 'bg-slate-500',
    attention: 'bg-orange-400',
};

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

export function shortId(id) {
    return String(id || '').slice(-8);
}

// Human-readable name for the model source, e.g. "org/model@main".
export function sourceDisplayName(item) {
    if (!item?.source) return '—';
    if (item.source.kind === 'huggingface' && item.source.huggingface) {
        const { repoId, revision } = item.source.huggingface;
        return revision && revision !== 'main' ? `${repoId}@${revision}` : repoId;
    }
    if (item.source.kind === 'model-catalog' && item.source.modelCatalog) {
        return item.source.modelCatalog.catalogEntryId;
    }
    return '—';
}

// Repository/catalog identifier for a cached model source, used to look the
// entry up in the (frontend-only) Model Market catalog.
export function sourceRepository(item) {
    if (!item?.source) return '';
    if (item.source.kind === 'huggingface' && item.source.huggingface) {
        return item.source.huggingface.repoId || '';
    }
    if (item.source.kind === 'model-catalog' && item.source.modelCatalog) {
        return item.source.modelCatalog.catalogEntryId || '';
    }
    return '';
}

export function sourceKindLabel(kind) {
    if (kind === 'huggingface') return 'HuggingFace';
    if (kind === 'model-catalog') return 'Model catalog';
    return kind || '—';
}

// Aggregated node progress, e.g. "2/3 ready" for local-disk entries.
export function nodeProgressSummary(item) {
    const nodes = item?.nodeProgress || [];
    if (!nodes.length) return '—';
    const ready = nodes.filter((node) => node.status === 'ready').length;
    return `${ready}/${nodes.length} node${nodes.length === 1 ? '' : 's'} ready`;
}

// True when a hostPath-backed entry's `pendingSyncNodes` (computed at
// read-time from `node_progress` vs the cluster's current node list) is
// non-empty — i.e. new cluster nodes joined after this model became ready
// and never got a download job. See `compute_pending_sync` in
// llm_d_bench/model_cache/service.py.
export function hasPendingSync(item) {
    return Array.isArray(item?.pendingSyncNodes) && item.pendingSyncNodes.length > 0;
}

export function pendingSyncMessage(item) {
    const nodes = item?.pendingSyncNodes || [];
    if (!nodes.length) return '';
    const list = nodes.join(', ');
    return `${nodes.length} new node${nodes.length === 1 ? '' : 's'} joined the cluster and ${nodes.length === 1 ? "hasn't" : "haven't"} `
        + `downloaded this model yet: ${list}. Use Sync to download it onto ${nodes.length === 1 ? 'that node' : 'those nodes'}.`;
}

// Folds `pendingSyncNodes` drift into the entry's own status field so the
// status dot reflects that the model is not fully replicated across the
// cluster, mirroring `effectiveStorageStatus` on the Storage page.
export function effectiveModelCacheStatus(item) {
    if (!item) return 'deleting';
    if (item.status === 'ready' && hasPendingSync(item)) return 'attention';
    return item.status;
}

export function summarizeStatuses(items) {
    const summary = { total: items.length, ready: 0, pending: 0, downloading: 0, failed: 0, deleting: 0 };
    for (const item of items) {
        const status = item?.status;
        if (status && Object.prototype.hasOwnProperty.call(summary, status)) summary[status] += 1;
    }
    return summary;
}

export function clusterOptions(items) {
    return [...new Set(items.map((item) => item?.clusterId).filter(Boolean))].sort();
}

export function storageVolumeOptions(items) {
    return [...new Set(items.map((item) => item?.storageVolumeId).filter(Boolean))].sort();
}

export { errorMessage } from '../../utils/errorMessage';
