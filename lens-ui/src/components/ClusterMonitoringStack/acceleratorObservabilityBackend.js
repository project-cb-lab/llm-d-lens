// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

const BASE_PATH = '/api/v1/monitoring/accelerators';

import { requestMonitoringJson as requestJson } from '../../api/monitoringClient';


export function getAcceleratorCapabilities({ signal } = {}) {
    return requestJson(BASE_PATH, { signal });
}

export function getAcceleratorStatus(accelerator, { namespace, accessMode, signal, clusterId } = {}) {
    const query = new URLSearchParams({ namespace });
    if (accessMode) query.set('access_mode', accessMode);
    if (clusterId) query.set('cluster_id', clusterId);
    return requestJson(`${BASE_PATH}/${encodeURIComponent(accelerator)}/status?${query.toString()}`, { signal });
}

export function preflightAcceleratorInstall(request, { signal, clusterId } = {}) {
    const accelerator = request.accelerator;
    const query = clusterId ? `?cluster_id=${encodeURIComponent(clusterId)}` : '';
    return requestJson(`${BASE_PATH}/${encodeURIComponent(accelerator)}/installations/preflight${query}`, {
        method: 'POST',
        signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
    });
}

export function installAccelerator(request, idempotencyKey, clusterId) {
    const accelerator = request.accelerator;
    const query = clusterId ? `?cluster_id=${encodeURIComponent(clusterId)}` : '';
    return requestJson(`${BASE_PATH}/${encodeURIComponent(accelerator)}/installations${query}`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': idempotencyKey,
        },
        body: JSON.stringify(request),
    });
}

export function getAcceleratorOperation(accelerator, operationId, { signal } = {}) {
    return requestJson(
        `${BASE_PATH}/${encodeURIComponent(accelerator)}/operations/${encodeURIComponent(operationId)}`,
        { signal }
    );
}

export function getAcceleratorLinks(accelerator, { signal, clusterId } = {}) {
    const query = clusterId ? `?cluster_id=${encodeURIComponent(clusterId)}` : '';
    return requestJson(`${BASE_PATH}/${encodeURIComponent(accelerator)}/links${query}`, { signal });
}
