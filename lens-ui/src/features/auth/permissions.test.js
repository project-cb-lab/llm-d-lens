import assert from 'node:assert/strict';
import test from 'node:test';

import { canAccessView, hasPermission, permissionMatches } from './permissions.js';

test('permissionMatches supports segment wildcards', () => {
    assert.equal(permissionMatches('deployment:run:read', 'deployment:run:read'), true);
    assert.equal(permissionMatches('deployment:*:*', 'deployment:run:create'), true);
    assert.equal(permissionMatches('*:*:read', 'cluster:cluster:read'), true);
    assert.equal(permissionMatches('deployment:run:read', 'deployment:run:create'), false);
    assert.equal(permissionMatches('deployment:run', 'deployment:run:create'), false);
});

test('hasPermission returns true when any grant matches', () => {
    const permissions = ['deployment:run:read', 'cluster:*:*'];
    assert.equal(hasPermission(permissions, 'cluster:cluster:delete'), true);
    assert.equal(hasPermission(permissions, 'storage:volume:delete'), false);
    assert.equal(hasPermission(permissions, ''), true);
});

test('canAccessView enforces the route catalog', () => {
    assert.equal(canAccessView(['deployment:run:read'], 'model-market'), true);
    assert.equal(canAccessView(['deployment:run:read'], 'admin/users'), false);
    assert.equal(canAccessView(['user:user:read'], 'admin/users'), true);
    assert.equal(canAccessView([], 'unknown-view'), true);
});

// Every sidebar entry in LeftNavigation must be permission-mapped (an unmapped
// view id would be visible to everyone because canAccessView allows unknowns).
const NAV_VIEWS = [
    'model-market', 'optimization-deployments', 'optimization-evaluate', 'optimization-simulate',
    'playground', 'clusters', 'storage-management', 'model-cache', 'ai-providers',
    'cluster-monitoring-stack', 'model-service', 'usage',
    'admin/users', 'admin/groups', 'admin/roles', 'admin/identity-providers', 'admin/sessions',
    'admin/audit', 'admin/model-service', 'admin/usage',
];

test('every sidebar view has a permission mapping', () => {
    for (const view of NAV_VIEWS) {
        assert.equal(canAccessView([], view), false, `${view} is not permission-mapped`);
    }
});

test('a model-service consumer only sees model-service and usage', () => {
    const consumer = ['model-service:token:manage', 'model-service:inference:use', 'model-service:usage:read'];
    for (const view of NAV_VIEWS) {
        const expected = view === 'model-service' || view === 'usage';
        assert.equal(canAccessView(consumer, view), expected, `${view} visibility for a model-service consumer`);
    }
});
