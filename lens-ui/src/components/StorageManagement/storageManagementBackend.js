// API client for the Storage module (GET/POST/DELETE /api/v1/storage/...).
// Mirrors the fetch/error-handling conventions used by remoteDeployBackend.js.

import { requestJson } from '../../api/httpClient';

export async function listStorageVolumes({ clusterId, kind, status, purpose, query, signal } = {}) {
    const params = new URLSearchParams();
    if (clusterId) params.set('cluster_id', clusterId);
    if (kind) params.set('kind', kind);
    if (status) params.set('status', status);
    if (purpose) params.set('purpose', purpose);
    if (query) params.set('query', query);
    const suffix = params.size ? `?${params.toString()}` : '';
    const payload = await requestJson(`/api/v1/storage/volumes${suffix}`, { signal });
    if (!Array.isArray(payload.items)) throw new Error('Storage volume list response is invalid');
    return payload.items;
}

export async function getStorageVolume(volumeId, { signal } = {}) {
    const payload = await requestJson(`/api/v1/storage/volumes/${encodeURIComponent(volumeId)}`, { signal });
    return payload;
}

export async function getStorageResourceStatuses(volumeIds, { signal } = {}) {
    const params = new URLSearchParams();
    for (const volumeId of volumeIds) params.append('volume_id', volumeId);
    if (!params.size) return {};
    const payload = await requestJson(`/api/v1/storage/volume-resource-status?${params.toString()}`, { signal });
    if (!payload.items || typeof payload.items !== 'object' || Array.isArray(payload.items)) {
        throw new Error('Storage resource status response is invalid');
    }
    return payload.items;
}

export async function createStorageVolume(payload) {
    const body = await requestJson('/api/v1/storage/volumes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    return body;
}

export async function deleteStorageVolume(volumeId, { keepModelFiles = false } = {}) {
    const params = new URLSearchParams({ keep_model_files: String(!!keepModelFiles) });
    const payload = await requestJson(`/api/v1/storage/volumes/${encodeURIComponent(volumeId)}?${params.toString()}`, {
        method: 'DELETE',
    });
    // 202: deletion started, volume now `status: "deleting"` while Model Cache
    // entries/files are cascaded and the underlying PV/PVC are torn down in
    // the background. 204 is kept for backwards compatibility with older
    // deployments still running the previous synchronous-delete API.
    return payload;
}

// Rebaselines a `local-disk` volume's node-drift baseline to the cluster's
// current nodes, clearing the "new node detected" warning after an operator
// has manually confirmed/replicated the hostPath content onto it.
export async function acknowledgeStorageVolumeNodes(volumeId) {
    const payload = await requestJson(`/api/v1/storage/volumes/${encodeURIComponent(volumeId)}/acknowledge-nodes`, {
        method: 'POST',
    });
    return payload;
}

export async function listStorageClasses({ clusterId, signal } = {}) {
    const params = new URLSearchParams({ cluster_id: clusterId || '' });
    const payload = await requestJson(`/api/v1/storage/storage-classes?${params.toString()}`, { signal });
    if (!Array.isArray(payload.items)) throw new Error('Storage class list response is invalid');
    return payload.items;
}

export async function listStorageNodes({ clusterId, signal } = {}) {
    const params = new URLSearchParams({ cluster_id: clusterId || '' });
    const payload = await requestJson(`/api/v1/storage/nodes?${params.toString()}`, { signal });
    if (!Array.isArray(payload.items)) throw new Error('Storage node list response is invalid');
    return payload.items;
}
