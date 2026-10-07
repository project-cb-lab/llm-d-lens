// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

const BASE_PATH = '/api/v1/monitoring/cluster-stack';

import { requestMonitoringJson as requestJson } from '../../api/monitoringClient';


function clusterQuery(namespace, clusterId) {
    const query = new URLSearchParams({ namespace });
    if (clusterId) query.set('cluster_id', clusterId);
    return query.toString();
}

export function getClusterStackStatus(namespace, { signal, clusterId } = {}) {
    return requestJson(`${BASE_PATH}/status?${clusterQuery(namespace, clusterId)}`, { signal });
}

export function preflightClusterStackInstall(request, { signal, clusterId } = {}) {
    const query = clusterId ? `?cluster_id=${encodeURIComponent(clusterId)}` : '';
    return requestJson(`${BASE_PATH}/installations/preflight${query}`, {
        method: 'POST',
        signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
    });
}

export function installClusterStack(request, idempotencyKey, clusterId) {
    const query = clusterId ? `?cluster_id=${encodeURIComponent(clusterId)}` : '';
    return requestJson(`${BASE_PATH}/installations${query}`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': idempotencyKey,
        },
        body: JSON.stringify(request),
    });
}

export function getClusterStackOperation(operationId, { signal } = {}) {
    return requestJson(`${BASE_PATH}/operations/${encodeURIComponent(operationId)}`, { signal });
}

export function getClusterStackLinks(namespace, { signal, clusterId } = {}) {
    return requestJson(`${BASE_PATH}/links?${clusterQuery(namespace, clusterId)}`, { signal });
}

export function getDeploymentFlowMap(executionId, { signal, clusterId } = {}) {
    const query = new URLSearchParams({ execution_id: executionId });
    if (clusterId) query.set('cluster_id', clusterId);
    return requestJson(`/api/v1/monitoring/profiling/flow-map?${query.toString()}`, { signal });
}

export function getClusterDeploymentMonitoring(clusterId, { signal } = {}) {
    return requestJson(
        `/api/v1/monitoring/deployments?cluster_id=${encodeURIComponent(clusterId)}`,
        { signal },
    );
}

export function getDeploymentMonitoring(executionId, { signal, clusterId } = {}) {
    const query = clusterId ? `?cluster_id=${encodeURIComponent(clusterId)}` : '';
    return requestJson(
        `/api/v1/monitoring/deployments/${encodeURIComponent(executionId)}${query}`,
        { signal },
    );
}

export function manageDeploymentMonitoring(executionId, action, { clusterId } = {}) {
    const query = clusterId ? `?cluster_id=${encodeURIComponent(clusterId)}` : '';
    return requestJson(
        `/api/v1/monitoring/deployments/${encodeURIComponent(executionId)}/${encodeURIComponent(action)}${query}`,
        { method: 'POST' },
    );
}
