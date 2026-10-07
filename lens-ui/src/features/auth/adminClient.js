/** Admin API client for users, groups, roles, identity providers and audit. */

import { postJson, requestJson } from '../../api/httpClient';

const json = (body) => ({ method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

function del(url) {
    return requestJson(url, { method: 'DELETE' });
}

// users
export function listUsers(params = {}) {
    const query = new URLSearchParams();
    if (params.query) query.set('query', params.query);
    if (params.status) query.set('status', params.status);
    const suffix = query.toString() ? `?${query.toString()}` : '';
    return requestJson(`/api/v1/users${suffix}`);
}
export function createUser(payload) {
    return postJson('/api/v1/users', payload);
}
export function updateUser(userId, payload) {
    return requestJson(`/api/v1/users/${userId}`, json(payload));
}
export function deleteUser(userId) {
    return del(`/api/v1/users/${userId}`);
}
export function resetUserPassword(userId, newPassword) {
    return postJson(`/api/v1/users/${userId}/password`, { newPassword });
}

// groups
export function listGroups() {
    return requestJson('/api/v1/groups');
}
export function createGroup(payload) {
    return postJson('/api/v1/groups', payload);
}
export function deleteGroup(groupId) {
    return del(`/api/v1/groups/${groupId}`);
}
export function listGroupMembers(groupId) {
    return requestJson(`/api/v1/groups/${groupId}/members`);
}
export function setGroupMembers(groupId, userIds) {
    return requestJson(`/api/v1/groups/${groupId}/members`, {
        ...json({ userIds }),
        method: 'PUT',
    });
}

// roles
export function listRoles() {
    return requestJson('/api/v1/roles');
}
export function createRole(payload) {
    return postJson('/api/v1/roles', payload);
}
export function setRolePermissions(roleId, permissions) {
    return requestJson(`/api/v1/roles/${roleId}/permissions`, {
        ...json({ permissions }),
        method: 'PUT',
    });
}
export function deleteRole(roleId) {
    return del(`/api/v1/roles/${roleId}`);
}
export function listPermissions() {
    return requestJson('/api/v1/auth/permissions');
}
export function addUserRoleBinding(userId, payload) {
    return postJson(`/api/v1/users/${userId}/role-bindings`, payload);
}
export function listUserRoleBindings(userId) {
    return requestJson(`/api/v1/users/${userId}/role-bindings`);
}
export function removeUserRoleBinding(userId, bindingId) {
    return del(`/api/v1/users/${userId}/role-bindings/${bindingId}`);
}
export function listGroupRoleBindings(groupId) {
    return requestJson(`/api/v1/groups/${groupId}/role-bindings`);
}
export function addGroupRoleBinding(groupId, payload) {
    return postJson(`/api/v1/groups/${groupId}/role-bindings`, payload);
}
export function removeGroupRoleBinding(groupId, bindingId) {
    return del(`/api/v1/groups/${groupId}/role-bindings/${bindingId}`);
}
export function listClusters() {
    return requestJson('/api/cluster/clusters');
}

// identity providers
export function listIdentityProviders() {
    return requestJson('/api/v1/identity-providers');
}
export function createIdentityProvider(payload) {
    return postJson('/api/v1/identity-providers', payload);
}
export function updateIdentityProvider(providerId, payload) {
    return requestJson(`/api/v1/identity-providers/${providerId}`, json(payload));
}
export function deleteIdentityProvider(providerId) {
    return del(`/api/v1/identity-providers/${providerId}`);
}
export function testIdentityProvider(providerId) {
    return postJson(`/api/v1/identity-providers/${providerId}/test`, {});
}
export function syncIdentityProvider(providerId) {
    return postJson(`/api/v1/identity-providers/${providerId}/sync`, {});
}

// stored-secret master key
export function getSecretKeyStatus() {
    return requestJson('/api/v1/system/secret-key');
}
export function rotateSecretKey(newKey) {
    return postJson('/api/v1/system/secret-key/rotate', newKey ? { newKey } : {});
}
export function clearOldSecretKeys() {
    return del('/api/v1/system/secret-key/old');
}

// sessions & audit
export function listMySessions() {
    return requestJson('/api/v1/auth/sessions');
}
// Returns the caller's sessions, or all sessions (with usernames) for admins.
export function listSessions() {
    return requestJson('/api/v1/sessions');
}
export function revokeSession(sessionId) {
    return del(`/api/v1/sessions/${sessionId}`);
}
export function listAuditLogs(params = {}) {
    const query = new URLSearchParams();
    if (params.eventType) query.set('event_type', params.eventType);
    if (params.actorUserId) query.set('actor_user_id', params.actorUserId);
    if (params.result) query.set('result', params.result);
    if (params.since) query.set('since', params.since);
    if (params.until) query.set('until', params.until);
    query.set('limit', String(params.limit ?? 100));
    query.set('offset', String(params.offset ?? 0));
    return requestJson(`/api/v1/audit-logs?${query.toString()}`);
}
