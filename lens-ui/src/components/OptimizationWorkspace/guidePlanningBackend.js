// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import { createResourceLoader } from '../../utils/resourceLoader';

import { requestJson as guidePlanningRequest } from '../../api/httpClient';

const catalogs = new Map();
const selectedClusterId = () => globalThis.sessionStorage?.getItem('prism_cluster_server_id') || '';

export function loadGuideCatalog({ clusterId = selectedClusterId(), refresh = false } = {}) {
    if (!catalogs.has(clusterId)) {
        catalogs.set(clusterId, createResourceLoader(() => guidePlanningRequest(
            `/api/guide-planning/catalog?${new URLSearchParams({ clusterId })}`,
        )));
    }
    return catalogs.get(clusterId)({ refresh });
}

export function planGuideDeployment(selection) {
    return guidePlanningRequest('/api/guide-planning/plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...selection, clusterId: selection.clusterId || (selection.clusterSessionId ? undefined : selectedClusterId()) }),
    });
}

// Start the source-only work while the user edits. It performs no deployment or
// cluster changes; plan() consumes the same in-flight/cached source when needed.
export function prepareGuideSource(selection) {
    return guidePlanningRequest('/api/guide-planning/prepare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...selection, clusterId: selection.clusterId || (selection.clusterSessionId ? undefined : selectedClusterId()) }),
    });
}
