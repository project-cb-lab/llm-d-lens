// API client for external AI providers.
import { requestJson } from '../../api/httpClient';

const endpoint = '/api/v1/ai-providers';
const idPath = (providerId) => `${endpoint}/${encodeURIComponent(providerId)}`;

export async function listAIProviders({ signal } = {}) {
    const payload = await requestJson(endpoint, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('AI provider list response is invalid');
    return payload.items;
}

export function getAIProvider(providerId, { signal } = {}) {
    return requestJson(idPath(providerId), { signal });
}

export function createAIProvider(payload) {
    return requestJson(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
}

export function updateAIProvider(providerId, payload) {
    return requestJson(idPath(providerId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
}

export async function deleteAIProvider(providerId) {
    await requestJson(idPath(providerId), { method: 'DELETE' });
}

export function testDraftAIProvider(payload) {
    return requestJson(`${endpoint}/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
}

export function testSavedAIProvider(providerId) {
    return requestJson(`${idPath(providerId)}/test`, { method: 'POST' });
}
