/**
 * Permission matching and the view -> required-permission catalog.
 *
 * Mirrors llm_d_bench/auth/permissions.py (segment wildcards). The catalog is
 * the single source for both route guarding and navigation filtering.
 */

export function permissionMatches(bound, required) {
    if (!bound || !required) return false;
    if (bound === required) return true;
    const boundParts = bound.split(':');
    const requiredParts = required.split(':');
    if (boundParts.length !== requiredParts.length) return false;
    return boundParts.every((part, index) => part === '*' || part === requiredParts[index]);
}

export function hasPermission(permissions = [], required) {
    if (!required) return true;
    return permissions.some((granted) => permissionMatches(granted, required));
}

// Any-of semantics: a view is visible when the principal holds one of these.
export const VIEW_PERMISSIONS = {
    'model-market': ['deployment:run:read'],
    'optimization-deployments': ['deployment:run:read'],
    'optimization-evaluate': ['evaluate:run:read'],
    // New-evaluation wizard: create a deploy-and-benchmark workflow, deploy an
    // edited configuration directly (Model Market advanced), or benchmark an
    // existing deployment (the wizard locks itself to that path without the first two).
    'optimization-evaluate-new': ['evaluate:workflow:create', 'deployment:run:create', 'evaluate:run:create'],
    'optimization-evaluation-details': ['evaluate:run:read'],
    'optimization-simulate': ['simulation:task:read'],
    playground: ['playground:chat:use'],
    clusters: ['cluster:cluster:read'],
    'storage-management': ['storage:volume:read'],
    'model-cache': ['model-cache:entry:read'],
    'ai-providers': ['ai-provider:provider:read'],
    'model-service': ['model-service:inference:use'],
    'api-keys': ['model-service:token:manage'],
    usage: ['model-service:usage:read'],
    'cluster-monitoring-stack': ['monitoring:cluster-stack:read', 'monitoring:accelerator:read'],
    'optimization-explore': ['configuration:artifact:read'],
    'optimization-deploy': ['deployment:run:read'],
    'optimization-workspace': ['configuration:artifact:read'],
    'opt-define': ['configuration:artifact:read'],
    'opt-search': ['configuration:artifact:read'],
    'opt-deploy': ['configuration:artifact:read'],
    'opt-benchmark': ['configuration:artifact:read'],
    'opt-performance': ['configuration:artifact:read'],
    'admin/users': ['user:user:read'],
    'admin/groups': ['group:group:read'],
    'admin/roles': ['role:role:read'],
    'admin/identity-providers': ['idp:provider:read'],
    'admin/master-key': ['system:secret:read'],
    'admin/sessions': ['session:session:read'],
    'admin/audit': ['audit:log:read'],
    'admin/model-service': ['model-service:group:manage', 'model-service:group:read', 'model-service:gateway:read'],
    'admin/usage': ['usage:report:read'],
};

export function canAccessView(permissions, view) {
    const requiredAny = VIEW_PERMISSIONS[view];
    if (!requiredAny) return true;
    return requiredAny.some((permission) => hasPermission(permissions, permission));
}
