// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import { createResourceLoader } from '../../utils/resourceLoader';
import { postJson, requestJson } from '../../api/httpClient';

async function postConfiguration(operation, body) {
    return postJson(`/api/v1/configurations/${operation}`, body);
}

export function resolveConfigurations(request) {
    return postConfiguration('resolve', request);
}

export function renderConfiguration(candidateConfig, render = {}) {
    return postConfiguration('render', {
        candidate_config: candidateConfig,
        render: { ...render, format: 'manifest' },
    });
}

export function saveConfiguration(deployableConfiguration, name) {
    return postConfiguration('save', {
        deployable_configuration: deployableConfiguration,
        file: { name },
    });
}

export const loadConfigurationCapabilities = createResourceLoader(() =>
    requestJson('/api/v1/configurations/capabilities')
);

export async function loadConfigurationArtifacts() {
    const payload = await requestJson('/api/v1/configurations/artifacts', {}, { fallback: [] });
    return Array.isArray(payload) ? payload : [];
}

export async function deleteConfigurationArtifact(artifactId) {
    await requestJson(`/api/v1/configurations/artifacts/${encodeURIComponent(artifactId)}`, { method: 'DELETE' });
}
