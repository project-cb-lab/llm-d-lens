---
name: auth
description: Use when adding, changing or deleting authenticated routes, permissions, roles, scopes, ownership, resource sharing, sessions or login/session behavior in the Python backend, Node gateway or browser UI.
---

# Auth (authentication & authorization)

Follow [workflow](../workflow/SKILL.md) for discovery, registration and
unresolved decisions. The authoritative design is
[`docs/design/auth-rbac-design.md`](../../../docs/design/auth-rbac-design.md);
decisions are numbered there (D1–D26) and the implementation status is in §21/§22.
All paths below are repo-relative.

## When to use

Any change that touches who may do what: a new/renamed/removed API route, a new
resource type, a new page or action button, role/permission changes, ownership or
sharing, session/login/CSRF, or an internal Node→Python call. Combine with
[backend](../backend/SKILL.md) for the service/router shape, [database](../database/SKILL.md)
for owner/binding schema, and [ui](../ui/SKILL.md) for pages, nav and gates.

## The model in one paragraph

Authorization is **action × scope**. The **action** is a permission code
`domain:resource:action` the caller's role must contain; the **scope** is *which
instances* (global / cluster / one resource) the binding, ownership or an explicit
share covers. Both gates must pass. Python is the authoritative policy point; the
Node gateway is the primary browser enforcement point; the UI only hides what the
user cannot do (never enforce in the UI alone).

Implementation map:

- Permission catalog + built-in roles: `llm_d_bench/auth/permissions.py`
  (`ALL_PERMISSIONS`, `BUILTIN_ROLE_PERMISSIONS`, `BUILTIN_ROLE_SELF_SCOPED`).
  Codes are **code, not a table** — never add a capability only to the DB.
- Decision core: `llm_d_bench/auth/policy.py` (`authorize`), scope helpers in
  `llm_d_bench/auth/scope.py`.
- Request helpers: `llm_d_bench/auth/access.py`
  (`current_principal`, `visible_cluster_ids`, `filter_by_cluster`,
  `require_cluster_access`, `owner_for_create`, `resource_readable`).
- Route registry + default-deny middleware: `llm_d_bench/auth/routes.py`,
  `llm_d_bench/auth/context.py` (CSRF double-submit on unsafe methods).
- Node gateway: `server/auth.ts`, `server/internalAuth.ts`,
  `server/backend-api-proxy.ts`.
- Browser: `src/api/httpClient.js`, `src/features/auth/`,
  `src/components/LeftNavigation.jsx`.

## Adding or changing a route (Python)

1. Put the handler in the owning domain router; the router-level permission comes
   from the registry, not from an inline `Depends`.
2. **Register the route** in `AUTH_ROUTE_PERMISSIONS` (`auth/routes.py`):
   `permission=None` = public, `""` = authenticated-only, otherwise a code in
   `ALL_PERMISSIONS`. Every non-whitelist route must have an entry — startup
   self-check fails fast otherwise. Missing the entry makes the route
   default-deny (`403 "route is not registered"`).
3. If the route is served by Node itself (not proxied), add it to
   `NODE_ROUTE_PERMISSIONS` (`server/auth.ts`) instead.
4. Enforce **resource scope**, not just the action: pass `current_principal(request)`
   and use `filter_by_cluster` (lists), `require_cluster_access` (target cluster),
   or `resource_readable(...)` (a concrete resource). A registered action alone
   only proves "may do this class of thing", not "may do it to this instance".
5. Server must derive identity/scope (cluster id, owner) from the record, never
   trust `clusterId`-style fields in the request body.
6. Add/adjust tests in `llm_d_bench/auth/` and the owning domain, and run the
   app import (startup self-check) plus `make test-python`.
7. **Removing a route or feature**: delete its registry entry (and
   `NODE_ROUTE_PERMISSIONS` if Node-native), its `VIEW_PERMISSIONS`/nav/page
   entries, and revoke any resource shares it owned. Unused permission codes may
   stay in the catalog, but a removed built-in-role capability is a human
   decision.

## Adding a resource type (ownership + sharing)

A new shareable/owned resource needs, in the same task:

1. Owner columns (`owner_user_id` / `owner_group_id`, `SET NULL`) via a reviewed
   migration ([database](../database/SKILL.md)).
2. Record the owner on create with `owner_for_create(current_principal(request))`
   (thread it through background work too — see `start_run(owner_user_id=...)`).
3. Read paths call `resource_readable(...)` with the resource's `owner_*` and the
   right permission; list paths use `filter_by_cluster` / `visible_cluster_ids`.
4. Visibility rule (design §7.6): `visible within scope ∪ created by self ∪ owner-group membership ∪ explicitly shared`.
5. If end-user actions on it are self-scoped, add the action to
   `_END_USER_SELF_SCOPED`; otherwise a cluster binding grants it on everyone's
   rows.
6. If it can be shared, add the resource type to `SHARE_RESOURCE_TYPES`
   (`auth/service.py`) and use the existing share endpoints/service
   (`share_resource` / `revoke_resource_shares` / `list_resource_shares`).
7. **Purge shares on delete**: call `AuthService.revoke_resource_shares(...)` for
   the removed ids (executions/runs) so a stale share cannot outlive the resource
   or hit a reused id. Cluster deletion already cascades via `scope_cluster_id`.
8. Register reusable capability in `.reuse/catalog.json` and run `reuse:check`.

## Self-scoped and creator-only rules (do not regress)

- An action in `BUILTIN_ROLE_SELF_SCOPED` is **not** granted by a cluster binding;
  it needs ownership or an explicit resource share.
- `OWNER_GROUP_EXCLUDED_ACTIONS` (`delete`, `share`, `grant-access`) are
  creator/maintainer-only: owner-group members and **resource shares** must not
  confer them.
- Casbin matcher uses `keyMatch`, **never `keyMatch2`** — codes are
  colon-delimited and keyMatch2 treats `:` as a wildcard, which leaks one
  resource's permission onto another.

## Node gateway and internal calls

- The gateway resolves the session (`fetchPrincipal`) and strips client-supplied
  `x-prism-internal-*` headers unless they carry a valid HMAC for that exact
  method+path. Never trust those headers from a client.
- Browser requests are proxied with cookie + `x-prism-csrf`; the proxy re-signs
  the caller for the canonical upstream path (`backend-api-proxy.ts`).
- **Node→Python internal reads must sign**: use `internalHeadersFor(method, path)`
  from a context set by `runWithInternalAuth` (see `server/clusterSources.ts`,
  `server/mcp/internal.ts`). Do not `fetch` the backend without auth. The
  Playground assistant follows the same rule when it calls `/api/mcp`; Node-native
  rules may accept an any-of list (e.g. `/api/mcp` accepts `mcp:tool:invoke` or
  `playground:chat:use`), because each tool action is still enforced by its target
  route permission.
- Never add a browser-reachable path to the public whitelist to make an internal
  call work; fix the call's auth instead.

## Browser / page rules

1. **Mutations must use the shared transport** (`requestJson` / `postJson`) so
   credentials and the CSRF header are sent. Never use a bare `fetch` with a
   non-GET method — the backend rejects it with `403 csrf_failed`.
2. Gate views in `VIEW_PERMISSIONS` (`src/features/auth/permissions.js`) and add
   the page to `SUPPORTED_VIEWS` (`src/App.jsx`) and the nav
   (`src/components/LeftNavigation.jsx`).
3. Gate actions with `PermissionGate` / `useAuth().can(...)`. UI gating is UX
   only — the backend still enforces.
4. When a page needs a picker/list that an end-user may not read via the admin
   APIs (users/groups/roles), add a purpose-limited, resource-scoped endpoint
   (see `.../access/options`) instead of opening the admin list.
5. Tag genuinely incomplete features with the existing `Exp` badge conventions
   (`LeftNavigation` item `badge`, `ModuleHeader badge`).

## Errors, CSRF and sessions

- Errors are RFC-7807 `application/problem+json` with a `code`. Unhandled
  exceptions now return `500` with `detail` (`<Type>: <message>`) and a
  `requestId` tied to the server traceback; the UI shows both. Never put secrets
  in exception messages.
- The Lens Assistant (`server/playground/chat.ts`) treats authorization errors as
  final: its system prompt tells the model to stop (not retry or try other tools)
  and tell the user which permission is missing. Keep that rule in sync when you
  change 401/403 problem codes, and prefer a deterministic stop in the orchestrator
  when you add new permission-denied paths.
- Unsafe methods require the double-submit CSRF header; do not weaken
  `context._verify_csrf`.
- `password_change_required` blocks everything except the password/session/logout
  paths; keep `_PASSWORD_CHANGE_ALLOWED` in sync if you add such a route.

## Human decisions — stop and ask

Pause and get explicit approval (and record it as a new `D<n>` in the design doc
§18) before:

- changing a built-in role's permissions or the meaning of a permission code;
- adding/removing a public route or an authentication-mode bypass;
- any schema change (owner columns, binding tables) — [database](../database/SKILL.md);
- changing share/grant semantics or who may grant privileged roles;
- exposing new error detail or internal identity material.

## Verify

- `make test-python` (auth + owning domain) and `make test-js`; `npm run type-check`.
- `npm run reuse:check -- --base <task-start-commit>`; update `.reuse/catalog.json`
  and regenerate `docs/reuse-map.md` when you add/extract a capability.
- Restart the backend (`scripts/dev.sh restart`) before manual verification:
  middleware, route registry and Node env changes are not hot-reloaded.
