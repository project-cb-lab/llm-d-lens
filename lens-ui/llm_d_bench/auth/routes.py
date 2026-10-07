"""Route -> permission registry (design section 8.4).

Every non-public backend route is mapped to exactly one permission code. The
guard treats an unregistered route as forbidden, and startup validation fails
fast if the FastAPI app exposes a route that is missing here (or vice versa).

``permission is None`` means public; ``permission == ""`` means "authenticated
but no specific permission required".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PUBLIC: None = None
AUTHENTICATED = ""

_PARAM_ALLOW_SLASH = {"repo_id", "rest"}

#: Methods Envoy's HTTP ext_authz may forward to the authorize endpoint.
_EXT_AUTH_METHODS = ("POST", "PUT", "PATCH", "DELETE", "GET")


@dataclass(frozen=True)
class RoutePermission:
    method: str
    path: str
    permission: str | None


def _regex_for(path_template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}]+\})", path_template)
    pattern = ""
    for part in parts:
        if part.startswith("{") and part.endswith("}"):
            name = part[1:-1].split(":", 1)[0]
            pattern += ".+" if name in _PARAM_ALLOW_SLASH else "[^/]+"
        else:
            pattern += re.escape(part)
    return re.compile("^" + pattern + "$")


ROUTE_PERMISSIONS: tuple[RoutePermission, ...] = (
    # --- public ---
    RoutePermission("GET", "/api/health", PUBLIC),
    RoutePermission("GET", "/healthz", PUBLIC),
    RoutePermission("GET", "/api/simulation/health", PUBLIC),
    RoutePermission("GET", "/api/simulation/docs", PUBLIC),
    RoutePermission("GET", "/api/simulation/redoc", PUBLIC),
    RoutePermission("GET", "/api/simulation/openapi.json", PUBLIC),
    RoutePermission("GET", "/docs/oauth2-redirect", PUBLIC),
    RoutePermission("GET", "/api/v1/auth/providers", PUBLIC),
    RoutePermission("POST", "/api/v1/auth/login", PUBLIC),
    RoutePermission("POST", "/api/v1/auth/bootstrap", PUBLIC),
    # --- auth session ---
    RoutePermission("POST", "/api/v1/auth/logout", AUTHENTICATED),
    RoutePermission("GET", "/api/v1/auth/session", AUTHENTICATED),
    RoutePermission("POST", "/api/v1/auth/password", AUTHENTICATED),
    RoutePermission("GET", "/api/v1/auth/sessions", AUTHENTICATED),
    RoutePermission("GET", "/api/v1/auth/permissions", "role:role:read"),
    RoutePermission("GET", "/api/v1/sessions", AUTHENTICATED),
    RoutePermission("DELETE", "/api/v1/sessions/{session_id}", AUTHENTICATED),
    # --- users ---
    RoutePermission("GET", "/api/v1/users", "user:user:read"),
    RoutePermission("POST", "/api/v1/users", "user:user:create"),
    RoutePermission("GET", "/api/v1/users/{user_id}", "user:user:read"),
    RoutePermission("PATCH", "/api/v1/users/{user_id}", "user:user:update"),
    RoutePermission("DELETE", "/api/v1/users/{user_id}", "user:user:delete"),
    RoutePermission("POST", "/api/v1/users/{user_id}/password", "user:user:reset-password"),
    RoutePermission("GET", "/api/v1/users/{user_id}/groups", "group:group:manage-members"),
    RoutePermission("PUT", "/api/v1/users/{user_id}/groups", "group:group:manage-members"),
    RoutePermission("GET", "/api/v1/users/{user_id}/role-bindings", "role:binding:manage"),
    RoutePermission("POST", "/api/v1/users/{user_id}/role-bindings", "role:binding:manage"),
    RoutePermission("DELETE", "/api/v1/users/{user_id}/role-bindings/{binding_id}", "role:binding:manage"),
    # --- groups ---
    RoutePermission("GET", "/api/v1/groups", "group:group:read"),
    RoutePermission("POST", "/api/v1/groups", "group:group:create"),
    RoutePermission("GET", "/api/v1/groups/{group_id}", "group:group:read"),
    RoutePermission("PATCH", "/api/v1/groups/{group_id}", "group:group:update"),
    RoutePermission("DELETE", "/api/v1/groups/{group_id}", "group:group:delete"),
    RoutePermission("GET", "/api/v1/groups/{group_id}/members", "group:group:manage-members"),
    RoutePermission("PUT", "/api/v1/groups/{group_id}/members", "group:group:manage-members"),
    RoutePermission("GET", "/api/v1/groups/{group_id}/role-bindings", "role:binding:manage"),
    RoutePermission("POST", "/api/v1/groups/{group_id}/role-bindings", "role:binding:manage"),
    RoutePermission("DELETE", "/api/v1/groups/{group_id}/role-bindings/{binding_id}", "role:binding:manage"),
    # --- roles ---
    RoutePermission("GET", "/api/v1/roles", "role:role:read"),
    RoutePermission("POST", "/api/v1/roles", "role:role:create"),
    RoutePermission("GET", "/api/v1/roles/{role_id}", "role:role:read"),
    RoutePermission("PATCH", "/api/v1/roles/{role_id}", "role:role:update"),
    RoutePermission("DELETE", "/api/v1/roles/{role_id}", "role:role:delete"),
    RoutePermission("PUT", "/api/v1/roles/{role_id}/permissions", "role:role:update"),
    # --- identity providers ---
    RoutePermission("GET", "/api/v1/identity-providers", "idp:provider:read"),
    RoutePermission("POST", "/api/v1/identity-providers", "idp:provider:configure"),
    RoutePermission("GET", "/api/v1/identity-providers/{provider_id}", "idp:provider:read"),
    RoutePermission("PATCH", "/api/v1/identity-providers/{provider_id}", "idp:provider:configure"),
    RoutePermission("DELETE", "/api/v1/identity-providers/{provider_id}", "idp:provider:configure"),
    RoutePermission("POST", "/api/v1/identity-providers/{provider_id}/test", "idp:provider:configure"),
    RoutePermission("POST", "/api/v1/identity-providers/{provider_id}/sync", "idp:sync:execute"),
    # --- audit ---
    RoutePermission("GET", "/api/v1/audit-logs", "audit:log:read"),
    # --- access management ---
    RoutePermission("GET", "/api/v1/clusters/{cluster_id}/access", "cluster:cluster:grant-access"),
    RoutePermission("POST", "/api/v1/clusters/{cluster_id}/access", "cluster:cluster:grant-access"),
    RoutePermission("DELETE", "/api/v1/clusters/{cluster_id}/access/{binding_id}", "cluster:cluster:grant-access"),
    # --- agentic deployments ---
    RoutePermission("GET", "/api/agentic-deployments", "deployment:agentic:read"),
    RoutePermission("POST", "/api/agentic-deployments", "deployment:agentic:create"),
    RoutePermission("POST", "/api/agentic-deployments/stream", "deployment:agentic:create"),
    RoutePermission("GET", "/api/agentic-deployments/{run_id}", "deployment:agentic:read"),
    RoutePermission("POST", "/api/agentic-deployments/{run_id}/approve", "deployment:agentic:approve"),
    RoutePermission("POST", "/api/agentic-deployments/{run_id}/refine", "deployment:agentic:create"),
    RoutePermission("POST", "/api/agentic-deployments/{run_id}/refine/stream", "deployment:agentic:create"),
    RoutePermission("POST", "/api/agentic-deployments/{run_id}/select", "deployment:agentic:create"),
    # --- capacity planning ---
    RoutePermission("POST", "/api/v1/capacity/estimate", "deployment:agentic:read"),
    # --- cluster ---
    RoutePermission("GET", "/api/cluster/clusters", "cluster:cluster:read"),
    RoutePermission("POST", "/api/cluster/clusters", "cluster:cluster:create"),
    RoutePermission("DELETE", "/api/cluster/clusters/{cluster_id}", "cluster:cluster:delete"),
    RoutePermission("PATCH", "/api/cluster/clusters/{cluster_id}", "cluster:cluster:update"),
    RoutePermission("POST", "/api/cluster/clusters/{cluster_id}/hf-token-secrets", "cluster:hf-token:create"),
    RoutePermission("POST", "/api/cluster/clusters/{cluster_id}/nodes/cordon", "cluster:node:operate"),
    RoutePermission("POST", "/api/cluster/clusters/{cluster_id}/nodes/uncordon", "cluster:node:operate"),
    RoutePermission("GET", "/api/cluster/clusters/{cluster_id}/software-downloads", "cluster:cluster:read"),
    RoutePermission("POST", "/api/cluster/clusters/{cluster_id}/software-downloads", "cluster:cluster:update"),
    RoutePermission("GET", "/api/cluster/clusters/{cluster_id}/images/prepull", "cluster:cluster:read"),
    RoutePermission("POST", "/api/cluster/clusters/{cluster_id}/images/prepull", "cluster:cluster:update"),
    RoutePermission("GET", "/api/cluster/clusters/{cluster_id}/crds", "cluster:cluster:read"),
    RoutePermission("POST", "/api/cluster/clusters/{cluster_id}/crds", "cluster:cluster:update"),
    RoutePermission("GET", "/api/cluster/clusters/{cluster_id}/kubernetes-version", "cluster:cluster:read"),
    RoutePermission("GET", "/api/cluster/endpoints", "cluster:endpoint:read"),
    RoutePermission("GET", "/api/cluster/lens-hosts", "cluster:cluster:read"),
    RoutePermission("GET", "/api/cluster/clusters/{cluster_id}/gateway-port-check", "cluster:cluster:update"),
    RoutePermission("GET", "/api/cluster/model-secrets", "cluster:cluster:read"),
    RoutePermission("GET", "/api/cluster/overview", "cluster:cluster:read"),
    RoutePermission("POST", "/api/cluster/port-forward", "cluster:port-forward:connect"),
    RoutePermission("DELETE", "/api/cluster/port-forwards/{forward_id}", "cluster:port-forward:connect"),
    RoutePermission("GET", "/api/cluster/session", "cluster:session:connect"),
    RoutePermission("GET", "/api/cluster/sessions/{session_id}", "cluster:cluster:read"),
    RoutePermission("GET", "/api/cluster/sessions/{session_id}/planning-discovery/{resource}", "cluster:planning:read"),
    RoutePermission("DELETE", "/api/cluster/sessions/{session_id}/port-forwards", "cluster:port-forward:connect"),
    RoutePermission("GET", "/api/cluster-overview/sessions/{session_id}", "cluster:cluster:read"),
    RoutePermission("POST", "/api/cluster-overview/sessions/{session_id}/disconnect", "cluster:session:connect"),
    # --- cluster bootstrap ---
    RoutePermission("POST", "/api/cluster/bootstrap", "cluster:bootstrap:execute"),
    RoutePermission("POST", "/api/cluster/bootstrap/host-keys", "cluster:bootstrap:execute"),
    RoutePermission("POST", "/api/cluster/bootstrap/preflight", "cluster:bootstrap:execute"),
    RoutePermission("GET", "/api/cluster/bootstrap/{bootstrap_id}", "cluster:bootstrap:read"),
    RoutePermission("POST", "/api/cluster/bootstrap/{bootstrap_id}/cancel", "cluster:bootstrap:execute"),
    # --- simulation ---
    RoutePermission("GET", "/api/simulation/backends", "simulation:task:read"),
    RoutePermission("POST", "/api/simulation/models/discover", "simulation:task:create"),
    RoutePermission("GET", "/api/simulation/scenarios", "simulation:task:read"),
    RoutePermission("GET", "/api/simulation/tasks", "simulation:task:read"),
    RoutePermission("POST", "/api/simulation/tasks", "simulation:task:create"),
    RoutePermission("DELETE", "/api/simulation/tasks/{task_id}", "simulation:task:delete"),
    RoutePermission("GET", "/api/simulation/tasks/{task_id}", "simulation:task:read"),
    RoutePermission("GET", "/api/simulation/tasks/{task_id}/artifacts/{kind}", "simulation:artifact:download"),
    RoutePermission("POST", "/api/simulation/tasks/{task_id}/rerun", "simulation:task:rerun"),
    RoutePermission("GET", "/api/simulation/tasks/{task_id}/response-code-issues", "simulation:task:read"),
    RoutePermission("POST", "/api/simulation/tasks/{task_id}/stop", "simulation:task:stop"),
    RoutePermission("GET", "/api/simulation/trace-datasets", "simulation:task:read"),
    RoutePermission("POST", "/api/simulation/trace-datasets/download", "simulation:dataset:download"),
    RoutePermission("GET", "/api/simulation/trace-datasets/{dataset_name}/timeline", "simulation:task:read"),
    # --- AI providers ---
    RoutePermission("GET", "/api/v1/ai-providers", "ai-provider:provider:read"),
    RoutePermission("POST", "/api/v1/ai-providers", "ai-provider:provider:create"),
    RoutePermission("POST", "/api/v1/ai-providers/test", "ai-provider:provider:test"),
    RoutePermission("DELETE", "/api/v1/ai-providers/{provider_id}", "ai-provider:provider:delete"),
    RoutePermission("GET", "/api/v1/ai-providers/{provider_id}", "ai-provider:provider:read"),
    RoutePermission("PUT", "/api/v1/ai-providers/{provider_id}", "ai-provider:provider:update"),
    RoutePermission("POST", "/api/v1/ai-providers/{provider_id}/chat/completions", "ai-provider:provider:chat"),
    RoutePermission("POST", "/api/v1/ai-providers/{provider_id}/test", "ai-provider:provider:test"),
    # --- AIC / candidate ---
    RoutePermission("POST", "/api/v1/aic/estimate", "candidate:candidate:search"),
    RoutePermission("POST", "/api/v1/aic/experiments", "candidate:candidate:search"),
    RoutePermission("POST", "/api/v1/aic/search", "candidate:candidate:search"),
    RoutePermission("POST", "/api/v1/aic/support", "candidate:candidate:search"),
    # --- configurations ---
    RoutePermission("GET", "/api/v1/configurations/artifacts", "configuration:artifact:read"),
    RoutePermission("DELETE", "/api/v1/configurations/artifacts/{artifact_id}", "configuration:artifact:delete"),
    RoutePermission("GET", "/api/v1/configurations/artifacts/{artifact_id}", "configuration:artifact:read"),
    RoutePermission("GET", "/api/v1/configurations/artifacts/{artifact_id}/bundle", "configuration:artifact:read"),
    RoutePermission("GET", "/api/v1/configurations/artifacts/{artifact_id}/manifest", "configuration:artifact:read"),
    RoutePermission("GET", "/api/v1/configurations/capabilities", "configuration:artifact:read"),
    RoutePermission("POST", "/api/v1/configurations/render", "configuration:artifact:render"),
    RoutePermission("POST", "/api/v1/configurations/resolve", "configuration:artifact:render"),
    RoutePermission("POST", "/api/v1/configurations/save", "configuration:artifact:save"),
    # --- deployments ---
    RoutePermission("GET", "/api/v1/deployments", "deployment:run:read"),
    RoutePermission("GET", "/api/v1/deployments/cluster/{cluster_id}", "deployment:run:read"),
    RoutePermission("GET", "/api/v1/deployments/executions", "deployment:run:read"),
    RoutePermission("DELETE", "/api/v1/deployments/executions/{execution_id}", "deployment:execution:delete"),
    RoutePermission("GET", "/api/v1/deployments/executions/{execution_id}", "deployment:run:read"),
    RoutePermission("PATCH", "/api/v1/deployments/executions/{execution_id}", "deployment:execution:update"),
    RoutePermission("POST", "/api/v1/deployments/executions/{execution_id}/endpoint", "deployment:execution:connect"),
    RoutePermission("GET", "/api/v1/deployments/executions/{execution_id}/pods", "deployment:run:read"),
    RoutePermission("GET", "/api/v1/deployments/executions/{execution_id}/pods/{pod_name}/logs", "deployment:run:read"),
    RoutePermission("GET", "/api/v1/deployments/runs", "deployment:run:read"),
    RoutePermission("POST", "/api/v1/deployments/runs", "deployment:run:create"),
    RoutePermission("DELETE", "/api/v1/deployments/runs/{run_id}", "deployment:execution:delete"),
    RoutePermission("GET", "/api/v1/deployments/runs/{run_id}", "deployment:run:read"),
    RoutePermission("POST", "/api/v1/deployments/runs/{run_id}/cancel", "deployment:run:cancel"),
    RoutePermission("GET", "/api/v1/deployments/runs/{run_id}/cases/{case_id}", "deployment:run:read"),
    RoutePermission("POST", "/api/v1/deployments/runs/{run_id}/cases/{case_id}/clean", "deployment:case:execute"),
    RoutePermission("GET", "/api/v1/deployments/runs/{run_id}/cases/{case_id}/logs", "deployment:run:read"),
    RoutePermission("POST", "/api/v1/deployments/runs/{run_id}/cases/{case_id}/refresh", "deployment:case:execute"),
    RoutePermission("POST", "/api/v1/deployments/runs/{run_id}/cases/{case_id}/restart", "deployment:case:execute"),
    RoutePermission("POST", "/api/v1/deployments/runs/{run_id}/cases/{case_id}/stop", "deployment:case:execute"),
    RoutePermission("POST", "/api/v1/deployments/runs/{run_id}/cluster-session", "deployment:session:rebind"),
    RoutePermission("POST", "/api/v1/deployments/standard-vllm-runs", "deployment:run:create"),
    RoutePermission("GET", "/api/v1/deployments/orphans", "deployment:run:read"),
    RoutePermission("POST", "/api/v1/deployments/orphans/clean", "deployment:execution:delete"),
    # --- deployment resource sharing ---
    RoutePermission("GET", "/api/v1/deployments/executions/{execution_id}/access", "deployment:run:share"),
    RoutePermission("POST", "/api/v1/deployments/executions/{execution_id}/access", "deployment:run:share"),
    RoutePermission(
        "DELETE", "/api/v1/deployments/executions/{execution_id}/access/{binding_id}", "deployment:run:share"
    ),
    RoutePermission("GET", "/api/v1/deployments/executions/{execution_id}/access/options", "deployment:run:share"),
    RoutePermission("GET", "/api/v1/deployments/runs/{run_id}/access", "deployment:run:share"),
    RoutePermission("POST", "/api/v1/deployments/runs/{run_id}/access", "deployment:run:share"),
    RoutePermission("DELETE", "/api/v1/deployments/runs/{run_id}/access/{binding_id}", "deployment:run:share"),
    RoutePermission("GET", "/api/v1/deployments/runs/{run_id}/access/options", "deployment:run:share"),
    # --- evaluate ---
    RoutePermission("GET", "/api/v1/evaluate/benchmark-defaults", "evaluate:run:read"),
    RoutePermission("POST", "/api/v1/evaluate/evaluations", "evaluate:workflow:create"),
    RoutePermission("GET", "/api/v1/evaluate/runs", "evaluate:run:read"),
    RoutePermission("POST", "/api/v1/evaluate/runs", "evaluate:run:create"),
    RoutePermission("DELETE", "/api/v1/evaluate/runs/{run_id}", "evaluate:run:delete"),
    RoutePermission("GET", "/api/v1/evaluate/runs/{run_id}", "evaluate:run:read"),
    RoutePermission("POST", "/api/v1/evaluate/runs/{run_id}/cancel", "evaluate:run:cancel"),
    RoutePermission("GET", "/api/v1/evaluate/workflow-runs", "evaluate:run:read"),
    RoutePermission("POST", "/api/v1/evaluate/workflow-runs", "evaluate:workflow:create"),
    RoutePermission("DELETE", "/api/v1/evaluate/workflow-runs/{workflow_id}", "evaluate:run:delete"),
    RoutePermission("GET", "/api/v1/evaluate/workflow-runs/{workflow_id}", "evaluate:run:read"),
    RoutePermission("POST", "/api/v1/evaluate/workflow-runs/{workflow_id}/cancel", "evaluate:run:cancel"),
    RoutePermission(
        "POST", "/api/v1/evaluate/workflow-runs/{workflow_id}/cases/{case_id}/cancel", "evaluate:run:cancel"
    ),
    RoutePermission("GET", "/api/v1/evaluate/workflow-runs/{workflow_id}/details", "evaluate:run:read"),
    RoutePermission("POST", "/api/v1/evaluate/workflow-runs/{workflow_id}/retry", "evaluate:run:retry"),
    # --- model cache ---
    RoutePermission("GET", "/api/v1/model-cache/entries", "model-cache:entry:read"),
    RoutePermission("POST", "/api/v1/model-cache/entries", "model-cache:entry:create"),
    RoutePermission("POST", "/api/v1/model-cache/entries/sync-nodes", "model-cache:entry:sync"),
    RoutePermission("DELETE", "/api/v1/model-cache/entries/{entry_id}", "model-cache:entry:delete"),
    RoutePermission("GET", "/api/v1/model-cache/entries/{entry_id}", "model-cache:entry:read"),
    RoutePermission("GET", "/api/v1/model-cache/entries/{entry_id}/logs", "model-cache:entry:read"),
    RoutePermission("POST", "/api/v1/model-cache/entries/{entry_id}/retry", "model-cache:entry:retry"),
    RoutePermission("POST", "/api/v1/model-cache/entries/{entry_id}/sync-nodes", "model-cache:entry:sync"),
    RoutePermission("GET", "/api/v1/model-cache/huggingface/models/{repo_id}", "model-cache:entry:read"),
    RoutePermission("GET", "/api/v1/model-cache/huggingface/search", "model-cache:entry:read"),
    # --- hardware profiles ---
    RoutePermission("GET", "/api/v1/hardware/capabilities", "hardware:profile:read"),
    # --- monitoring: accelerators ---
    RoutePermission("GET", "/api/v1/monitoring/accelerators", "monitoring:accelerator:read"),
    RoutePermission(
        "POST", "/api/v1/monitoring/accelerators/{accelerator}/installations", "monitoring:accelerator:install"
    ),
    RoutePermission(
        "POST",
        "/api/v1/monitoring/accelerators/{accelerator}/installations/preflight",
        "monitoring:accelerator:install",
    ),
    RoutePermission("GET", "/api/v1/monitoring/accelerators/{accelerator}/links", "monitoring:accelerator:read"),
    RoutePermission(
        "GET",
        "/api/v1/monitoring/accelerators/{accelerator}/operations/{operation_id}",
        "monitoring:operation:read",
    ),
    RoutePermission("GET", "/api/v1/monitoring/accelerators/{accelerator}/status", "monitoring:accelerator:read"),
    # --- monitoring: cluster stack ---
    RoutePermission("POST", "/api/v1/monitoring/cluster-stack/installations", "monitoring:cluster-stack:install"),
    RoutePermission(
        "POST", "/api/v1/monitoring/cluster-stack/installations/preflight", "monitoring:cluster-stack:install"
    ),
    RoutePermission("GET", "/api/v1/monitoring/cluster-stack/links", "monitoring:cluster-stack:read"),
    RoutePermission("GET", "/api/v1/monitoring/cluster-stack/operations/{operation_id}", "monitoring:operation:read"),
    RoutePermission("GET", "/api/v1/monitoring/cluster-stack/status", "monitoring:cluster-stack:read"),
    # --- monitoring: deployments / gpu / profiling ---
    RoutePermission("GET", "/api/v1/monitoring/deployments", "monitoring:deployment:read"),
    RoutePermission("GET", "/api/v1/monitoring/deployments/{execution_id}", "monitoring:deployment:read"),
    RoutePermission("POST", "/api/v1/monitoring/deployments/{execution_id}/{action}", "monitoring:deployment:action"),
    RoutePermission("POST", "/api/v1/monitoring/gpu-driver/install", "monitoring:gpu-driver:install"),
    RoutePermission("GET", "/api/v1/monitoring/gpu-driver/status", "monitoring:gpu-driver:read"),
    RoutePermission("GET", "/api/v1/monitoring/profiling/flow-map", "monitoring:profiling:read"),
    # --- storage ---
    RoutePermission("GET", "/api/v1/storage/nodes", "storage:volume:read"),
    RoutePermission("GET", "/api/v1/storage/storage-classes", "storage:volume:read"),
    RoutePermission("GET", "/api/v1/storage/volume-resource-status", "storage:volume:read"),
    RoutePermission("GET", "/api/v1/storage/volumes", "storage:volume:read"),
    RoutePermission("POST", "/api/v1/storage/volumes", "storage:volume:create"),
    RoutePermission("DELETE", "/api/v1/storage/volumes/{volume_id}", "storage:volume:delete"),
    RoutePermission("GET", "/api/v1/storage/volumes/{volume_id}", "storage:volume:read"),
    RoutePermission("POST", "/api/v1/storage/volumes/{volume_id}/acknowledge-nodes", "storage:volume:acknowledge"),
    RoutePermission("POST", "/api/v1/storage/volumes/{volume_id}/scan-models", "storage:volume:scan"),
    # --- system ---
    RoutePermission("GET", "/api/v1/system/database", "system:database:read"),
    RoutePermission("POST", "/api/v1/system/database", "system:database:configure"),
    RoutePermission("GET", "/api/v1/system/secret-key", "system:secret:read"),
    RoutePermission("POST", "/api/v1/system/secret-key/rotate", "system:secret:manage"),
    RoutePermission("DELETE", "/api/v1/system/secret-key/old", "system:secret:manage"),
    # --- versions ---
    RoutePermission("GET", "/api/v1/versions", AUTHENTICATED),
    # --- model service: user ---
    RoutePermission("GET", "/api/v1/model-service/tokens", "model-service:token:manage"),
    RoutePermission("POST", "/api/v1/model-service/tokens", "model-service:token:manage"),
    RoutePermission("POST", "/api/v1/model-service/tokens/regenerate", "model-service:token:manage"),
    RoutePermission("DELETE", "/api/v1/model-service/tokens/{token_id}", "model-service:token:manage"),
    RoutePermission("GET", "/api/v1/model-service/models", "model-service:inference:use"),
    RoutePermission("GET", "/api/v1/model-service/connection", "model-service:inference:use"),
    RoutePermission("GET", "/api/v1/model-service/usage/records", "model-service:usage:read"),
    RoutePermission("GET", "/api/v1/model-service/usage/timeseries", "model-service:usage:read"),
    # --- model service: admin ---
    RoutePermission("GET", "/api/v1/model-service/admin/groups", "model-service:group:read"),
    RoutePermission("POST", "/api/v1/model-service/admin/groups", "model-service:group:manage"),
    RoutePermission("GET", "/api/v1/model-service/admin/groups/{group_id}", "model-service:group:read"),
    RoutePermission("PATCH", "/api/v1/model-service/admin/groups/{group_id}", "model-service:group:manage"),
    RoutePermission("DELETE", "/api/v1/model-service/admin/groups/{group_id}", "model-service:group:manage"),
    RoutePermission("GET", "/api/v1/model-service/admin/members", "model-service:group:read"),
    RoutePermission("GET", "/api/v1/model-service/admin/deployments", "model-service:group:read"),
    RoutePermission("POST", "/api/v1/model-service/admin/members", "model-service:member:manage"),
    RoutePermission("PATCH", "/api/v1/model-service/admin/members/{member_id}", "model-service:member:manage"),
    RoutePermission("DELETE", "/api/v1/model-service/admin/members/{member_id}", "model-service:member:manage"),
    RoutePermission("GET", "/api/v1/model-service/admin/usage", "usage:report:read"),
    RoutePermission("GET", "/api/v1/model-service/admin/usage/analytics", "usage:report:read"),
    # --- model service: gateway operations (shared Gateway) ---
    RoutePermission("GET", "/api/v1/model-service/admin/gateway/status", "model-service:gateway:read"),
    RoutePermission("POST", "/api/v1/model-service/admin/gateway/reconcile", "model-service:gateway:manage"),
    RoutePermission("POST", "/api/v1/model-service/admin/gateway/probe", "model-service:gateway:manage"),
    RoutePermission("POST", "/api/v1/model-service/admin/gateway/usage/sync", "model-service:gateway:manage"),
    RoutePermission("GET", "/api/v1/model-service/admin/gateway/ipp-config", "model-service:gateway:read"),
    RoutePermission("POST", "/api/v1/model-service/admin/gateway/ipp-config", "model-service:gateway:manage"),
    RoutePermission("POST", "/api/v1/model-service/admin/gateway/preflight", "model-service:router:manage"),
    RoutePermission("POST", "/api/v1/model-service/admin/gateway/install", "model-service:gateway:manage"),
    RoutePermission("POST", "/api/v1/model-service/admin/gateway/scale", "model-service:gateway:manage"),
    RoutePermission("POST", "/api/v1/model-service/admin/gateway/logs", "model-service:gateway:read"),
    RoutePermission("POST", "/api/v1/model-service/admin/gateway/stream", "model-service:gateway:manage"),
    # --- model service: internal ---
    # Envoy forwards the client's HTTP method to the ext_authz service, so the
    # authorize paths accept every forwarded method.
    *(RoutePermission(method, "/api/v1/internal/model-gateway/authorize", PUBLIC) for method in _EXT_AUTH_METHODS),
    *(
        RoutePermission(method, "/api/v1/internal/model-gateway/authorize/{rest:path}", PUBLIC)
        for method in _EXT_AUTH_METHODS
    ),
    RoutePermission("POST", "/api/v1/internal/model-gateway/record-usage", PUBLIC),
    RoutePermission("GET", "/api/v1/internal/model-gateway/models", AUTHENTICATED),
    # --- model service: OpenAI-compatible public surface for model tokens ---
    RoutePermission("GET", "/v1/models", PUBLIC),
)

_COMPILED: tuple[tuple[str, re.Pattern[str], str | None], ...] = tuple(
    (entry.method, _regex_for(entry.path), entry.permission) for entry in ROUTE_PERMISSIONS
)


def resolve_route_permission(method: str, path: str) -> tuple[bool, str | None]:
    """Return ``(found, permission)`` for a concrete request path."""
    upper = method.upper()
    for entry_method, pattern, permission in _COMPILED:
        if entry_method == upper and pattern.match(path):
            return True, permission
    return False, None


#: Routes that are not part of the OpenAPI schema but are still served.
_NON_SCHEMA_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/health"),
    ("GET", "/healthz"),
    ("GET", "/api/simulation/health"),
    ("GET", "/api/simulation/docs"),
    ("GET", "/api/simulation/redoc"),
    ("GET", "/api/simulation/openapi.json"),
    ("GET", "/docs/oauth2-redirect"),
)


def app_route_table(app: object) -> set[tuple[str, str]]:
    """Concrete ``(method, path)`` pairs exposed by the app.

    OpenAPI supplies schema routes with correct prefixes (including nested
    ``include_router`` calls); the explicit extras cover health aliases and
    documentation routes that are excluded from the schema.
    """
    spec = app.openapi()  # type: ignore[attr-defined]
    table: set[tuple[str, str]] = set()
    for path, operations in spec.get("paths", {}).items():
        for method in operations:
            if method.lower() in {"parameters", "head", "options"}:
                continue
            table.add((method.upper(), path))
    table.update(_NON_SCHEMA_ROUTES)
    return table


def validate_route_registry(app: object) -> list[str]:
    """Fail-fast check: every served route must have a registry entry.

    Extra registry entries are tolerated (e.g. docs routes when documentation
    is disabled); an unregistered *served* route is a default-deny hazard.
    """
    app_routes = app_route_table(app)
    registry = {(entry.method, entry.path) for entry in ROUTE_PERMISSIONS}
    return [f"route not registered: {method} {path}" for method, path in sorted(app_routes - registry)]
