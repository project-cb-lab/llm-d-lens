// API client for the model service domain (tokens, visible models, usage).
import { requestJson } from '../../api/httpClient';
import { streamSseRequest } from '../../api/sse.js';

const base = '/api/v1/model-service';
const tokenPath = (tokenId) => `${base}/tokens/${encodeURIComponent(tokenId)}`;
const jsonHeaders = { 'Content-Type': 'application/json' };

export async function listModelTokens({ signal } = {}) {
    const payload = await requestJson(`${base}/tokens`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Model token list response is invalid');
    return payload.items;
}

export function createModelToken(name) {
    return requestJson(`${base}/tokens`, {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify({ name }),
    });
}

export function regenerateModelToken(name) {
    return requestJson(`${base}/tokens/regenerate`, {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify({ name }),
    });
}

export async function revokeModelToken(tokenId) {
    await requestJson(tokenPath(tokenId), { method: 'DELETE' });
}

export async function listModelNames({ signal } = {}) {
    const payload = await requestJson(`${base}/models`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Model list response is invalid');
    return payload.items;
}

export function getModelServiceConnection({ signal } = {}) {
    return requestJson(`${base}/connection`, { signal });
}

export async function listUsageRecords({ limit = 100, signal } = {}) {
    const payload = await requestJson(`${base}/usage/records?limit=${encodeURIComponent(limit)}`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Usage response is invalid');
    return payload.items;
}

export async function listUsageTimeseries({ days = 30, signal } = {}) {
    const payload = await requestJson(`${base}/usage/timeseries?days=${encodeURIComponent(days)}`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Usage timeseries response is invalid');
    return payload.items;
}

// --- admin -------------------------------------------------------------------

const admin = `${base}/admin`;

export async function listGroups({ signal } = {}) {
    const payload = await requestJson(`${admin}/groups`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Group list response is invalid');
    return payload.items;
}

export function createGroup(body) {
    return requestJson(`${admin}/groups`, { method: 'POST', headers: jsonHeaders, body: JSON.stringify(body) });
}

export function updateGroup(groupId, body) {
    return requestJson(`${admin}/groups/${encodeURIComponent(groupId)}`, {
        method: 'PATCH', headers: jsonHeaders, body: JSON.stringify(body),
    });
}

export async function deleteGroup(groupId) {
    await requestJson(`${admin}/groups/${encodeURIComponent(groupId)}`, { method: 'DELETE' });
}

export async function listMembers(groupId, { signal } = {}) {
    const query = groupId ? `?group_id=${encodeURIComponent(groupId)}` : '';
    const payload = await requestJson(`${admin}/members${query}`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Member list response is invalid');
    return payload.items;
}

export async function listPublishableDeployments({ signal } = {}) {
    const payload = await requestJson(`${admin}/deployments`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Deployments response is invalid');
    return payload.items;
}

export function createMember(body) {
    return requestJson(`${admin}/members`, { method: 'POST', headers: jsonHeaders, body: JSON.stringify(body) });
}

export function updateMember(memberId, body) {
    return requestJson(`${admin}/members/${encodeURIComponent(memberId)}`, {
        method: 'PATCH', headers: jsonHeaders, body: JSON.stringify(body),
    });
}

export async function deleteMember(memberId) {
    await requestJson(`${admin}/members/${encodeURIComponent(memberId)}`, { method: 'DELETE' });
}

export function getUsageAnalytics({ since, until, interval = 'day', groupBy = 'model', userIds, groupIds, clusterIds, signal } = {}) {
    const params = new URLSearchParams({ interval, group_by: groupBy });
    if (since) params.set('since', since);
    if (until) params.set('until', until);
    (userIds || []).forEach((id) => params.append('user_id', id));
    (groupIds || []).forEach((id) => params.append('group_id', id));
    (clusterIds || []).forEach((id) => params.append('cluster_id', id));
    return requestJson(`${admin}/usage/analytics?${params.toString()}`, { signal });
}

export async function listAllUsage({ groupIds, clusterIds, limit = 100, signal } = {}) {
    const params = new URLSearchParams();
    (groupIds || []).forEach((id) => params.append('group_id', id));
    (clusterIds || []).forEach((id) => params.append('cluster_id', id));
    params.set('limit', String(limit));
    const payload = await requestJson(`${admin}/usage?${params.toString()}`, { signal });
    if (!Array.isArray(payload?.items)) throw new Error('Admin usage response is invalid');
    return payload.items;
}

// --- gateway operations ------------------------------------------------------

const gateway = `${admin}/gateway`;

export function getGatewayStatus({ signal } = {}) {
    return requestJson(`${gateway}/status`, { signal });
}

// Reconcile every cluster with members: ensure the shared Gateway and each
// model service's HTTPRoute/IPP. Publishing/removing members triggers this
// automatically; this is the manual retry.
export function reconcileGateway() {
    return requestJson(`${gateway}/reconcile`, { method: 'POST', headers: jsonHeaders, body: JSON.stringify({}) });
}

export function probeGateway() {
    return requestJson(`${gateway}/probe`, { method: 'POST', headers: jsonHeaders, body: JSON.stringify({}) });
}

export function preflightCluster(clusterId) {
    return requestJson(`${gateway}/preflight`, {
        method: 'POST', headers: jsonHeaders, body: JSON.stringify({ clusterId }),
    });
}

// Install (or reuse) the cluster's shared Gateway using the user-selected provider.
export function installGateway(clusterId, provider) {
    return requestJson(`${gateway}/install`, {
        method: 'POST', headers: jsonHeaders, body: JSON.stringify({ clusterId, provider }),
    });
}

// Read a cluster's live Inference Payload Processor config (from its ConfigMap).
export function getGatewayIppConfig(clusterId) {
    const query = new URLSearchParams({ clusterId });
    return requestJson(`${gateway}/ipp-config?${query.toString()}`);
}

// Save a cluster's Inference Payload Processor config (PayloadProcessorConfig body).
export function setGatewayIppConfig(clusterId, config) {
    return requestJson(`${gateway}/ipp-config`, {
        method: 'POST', headers: jsonHeaders, body: JSON.stringify({ clusterId, config }),
    });
}

// Start/stop a data-plane component Deployment (Gateway data plane / EPP / model server).
export function scaleComponent({ clusterId, namespace, name, replicas, kind = 'deployment' }) {
    return requestJson(`${gateway}/scale`, {
        method: 'POST', headers: jsonHeaders, body: JSON.stringify({ clusterId, namespace, name, replicas, kind }),
    });
}

// Run any data-plane operation and stream its log lines (SSE `log`/`complete`/`error`).
export function streamGatewayOperation(payload, onEvent, signal) {
    return streamSseRequest(`${gateway}/stream`, payload, onEvent, signal);
}

export function streamComponentLogs(payload, onEvent, signal) {
    return streamSseRequest(`${gateway}/logs`, payload, onEvent, signal);
}

