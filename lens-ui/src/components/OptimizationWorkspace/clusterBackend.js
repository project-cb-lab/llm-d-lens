// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import { requestJson as clusterRequest } from '../../api/httpClient';

export function loadClusters({ signal } = {}) {
    return clusterRequest('/api/cluster/clusters', { signal });
}

export async function loadCluster(clusterId) {
    const payload = await loadClusters();
    const cluster = (payload?.items || []).find((item) => item.id === clusterId);
    if (!cluster) throw new Error(`Cluster ${clusterId} was not found`);
    return cluster;
}

export async function listLensHosts() {
    const payload = await clusterRequest('/api/cluster/lens-hosts');
    return Array.isArray(payload?.items) ? payload.items : [];
}

export function selectCluster(clusterId) {
    const query = new URLSearchParams({ clusterId });
    return clusterRequest(`/api/cluster/session?${query.toString()}`);
}

export function loadClusterOverview(clusterId) {
    const query = new URLSearchParams({ clusterId });
    return clusterRequest(`/api/cluster/overview?${query.toString()}`);
}

export function loadModelSecrets(clusterId) {
    const query = new URLSearchParams({ clusterId });
    return clusterRequest(`/api/cluster/model-secrets?${query.toString()}`);
}

export async function openClusterSession(clusterId) {
    const payload = await selectCluster(clusterId);
    const sessionId = payload.sessionId || payload.session_id;
    if (!sessionId) throw new Error('The selected cluster did not return an active session');
    return sessionId;
}

export async function waitForSoftwareDownloads(clusterId, { onUpdate, timeoutMs = 300000, intervalMs = 2000 } = {}) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        const status = await clusterRequest(`/api/cluster/clusters/${encodeURIComponent(clusterId)}/software-downloads`);
        onUpdate?.(status);
        const pending = ['llmD', 'llmDBenchmark'].some((key) => status[key]?.state === 'downloading');
        if (!pending) return status;
        if (Date.now() > deadline) {
            throw new Error('Timed out waiting for llm-d / llm-d-benchmark downloads to finish');
        }
        await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
}

export function loadStackVersions() {
    return clusterRequest('/api/v1/versions');
}

export function loadClusterKubernetesVersion(clusterId) {
    return clusterRequest(`/api/cluster/clusters/${encodeURIComponent(clusterId)}/kubernetes-version`);
}

export function loadClusterCrds(clusterId) {
    return clusterRequest(`/api/cluster/clusters/${encodeURIComponent(clusterId)}/crds`);
}

export function applyClusterCrds(clusterId) {
    return clusterRequest(`/api/cluster/clusters/${encodeURIComponent(clusterId)}/crds`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
    });
}

export function startClusterImagePrepull(clusterId, accelerators) {
    return clusterRequest(`/api/cluster/clusters/${encodeURIComponent(clusterId)}/images/prepull`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ accelerators }),
    });
}

export async function waitForClusterImagePrepull(clusterId, { onUpdate, timeoutMs = 1800000, intervalMs = 3000 } = {}) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        const status = await clusterRequest(`/api/cluster/clusters/${encodeURIComponent(clusterId)}/images/prepull`);
        onUpdate?.(status);
        if (['ready', 'idle', 'failed'].includes(status?.state)) return status;
        if (Date.now() > deadline) throw new Error('Timed out waiting for the image pre-pull to finish');
        await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
}

export async function downloadClusterSoftware(cluster, options = {}) {
    // The backend downloads the revisions pinned by the Lens stack profile; the
    // request body is intentionally empty.
    await clusterRequest(`/api/cluster/clusters/${encodeURIComponent(cluster.id)}/software-downloads`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
    });
    const status = await waitForSoftwareDownloads(cluster.id, options);
    const failed = ['llmD', 'llmDBenchmark'].filter((key) => status[key]?.state === 'failed');
    if (failed.length) {
        throw new Error(failed.map((key) => `${key === 'llmD' ? 'llm-d' : 'llm-d-benchmark'} download failed: ${status[key].error}`).join('; '));
    }
    return {
        ...cluster,
        llmDRepoPath: status.llmD?.path ?? cluster.llmDRepoPath,
        llmDBenchmarkRepoPath: status.llmDBenchmark?.path ?? cluster.llmDBenchmarkRepoPath,
    };
}
