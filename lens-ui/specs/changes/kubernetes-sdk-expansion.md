# Kubernetes SDK expansion

User direction: migrate as much of the hybrid Kubernetes integration as possible,
not only the first resource-list pilot. Preserve unrelated in-progress work.

## Reuse and boundaries

| Capability | Existing candidate | Implementation decision |
| --- | --- | --- |
| Cluster identity | `kubeconfig_environment`, registry and sessions | Reuse with strict resolution in SDK mode. |
| Resource reads | `kubernetes_reads.list_sdk_resources` | Extend through shared raw SDK API and discovery. |
| Existing caller result contracts | `CommandResult`, scoped runners | Adapt a finite set of known command forms to typed operations; unsupported syntax chooses CLI before side effects. |
| Deployment policy | `RestrictedKubectlRunner._validate` | Keep validation before transport selection. |
| Credential execution | SDK loader + `utils.shell.spawn` | Add bounded sanitized exec credentials and deterministic cleanup. |
| Node discovery | `planningDiscovery`, session registry | Session-only backend reuse; retain Node-host ambient context behavior. |
| Manifest deployment | Existing kubectl apply and Helm/Kustomize | Retain client-side apply semantics; do not silently change field ownership to server-side apply. |

The new adapter does not interpret arbitrary shell scripts. It accepts only
explicitly recognized argv forms, preserves success/failure contracts and never
retries failed SDK requests using kubectl. Typed API errors remain available at
the lower-level API; legacy callers retain their current error/empty-list contracts.

## Execution plan

- [x] Expand raw API resource lookup, named/list queries, version and log reads.
- [x] Implement safe exec authentication with setup-failure/cancellation tests.
- [x] Add create, patch, label, scale and delete helpers, preserving conflicts and wait semantics.
- [x] Integrate finite adapters into scoped and restricted runners after validation.
- [x] Reuse Python selected-session discovery from Node without accepting user file paths.
- [x] Add lifecycle-managed per-cluster connections and bounded watch transport.
- [x] Run relevant caller tests, local real-SDK fixtures, lint and full reuse report.
- [x] Update capability catalog and operating documentation with exact retained CLI cases.

Task-start commit: `c708b9ee4c642ecd2040798acc693e2b9bdc82b8`.
Tests may use isolated local API servers; no live mutations are part of verification.

## Delivery and verification

- `PRISM_KUBERNETES_BACKEND=sdk` enables migrated reads and writes. Legacy
  `PRISM_KUBERNETES_READ_BACKEND` remains read-only when explicitly set. Following
  the user’s request to finish switching defaults, unset variables now select SDK
  reads and writes. Explicit BACKEND=cli restores CLI. No process environment
  change/service restart performed.
- Real SDK tests cover HTTP, mutual TLS, exec subprocess descendants, credential
  refresh, connection pool cleanup, CRUD, pagination, Watch and caller integration.
- Full Python run: 1224 passed, four failures in unchanged functions. AST comparison
  against task-start HEAD confirms identical `endpoint_smoke_test`,
  `_validate_evaluation_capacity`, and `_shared_prefix_workload_yaml`. Failures are
  the smoke endpoint hostname expectation, capacity expectation, and two workload
  output expectations. They were not weakened or changed by this task.
- Node planning tests pass (three); TypeScript type-check passes. Focused new
  Python modules and tests pass Ruff.
- Live **read-only** SDK/kubectl comparison using the existing default kubeconfig:
  matching server version and matching five-node name/UID sets. No live writes.
- Independent review identified exec descendant cleanup, watch pool starvation and
  credential refresh issues; fixed with regression tests before delivery.
- Reuse check report: `.cache/reuse/kubernetes-expansion-report.json`, zero
  structural errors. Existing unrelated pending DB type ownership decision
  `ca783e76-c2cd-4880-b745-0f73ee1e5ff2` leaves global status WAITING_FOR_USER.
  Reviewed all 94 reported pairs. New adapter option dictionary vs evaluation metric
  units dictionary is structural overlap with unrelated semantics, not reusable
  behavior. Monitoring operation orchestration overlap predates this transport
  change and retains distinct domain errors/types. Other pairs are outside this
  task's changed behavior (concurrent frontend/storage/artifact work). No pending
  decision was resolved or suppressed, and no unrelated refactor was performed.

## Remaining explicit boundaries

Client-side apply, Helm/Kustomize, exec/port-forward, unsupported advanced CLI flags,
remote SSH commands, and legacy auth-provider stay CLI. The old list facade still
returns empty lists on request errors; strict API functions expose typed errors for
callers that need them. Changing all UI/error business contracts is not bundled
into transport replacement. See [operating guide](../../docs/kubernetes-read-backend.md).

## Default SDK follow-up

The user approved making migrated operations use SDK by default. Reused
`sdk_enabled` and `sdk_writes_enabled`; no new capability or parallel selector
was introduced. Both unset variables now enable SDK reads/writes. Explicit
BACKEND overrides READ_BACKEND; legacy-only settings preserve their previous
read-only behavior.

Validation: 163 focused Python tests passed, including real local HTTP reads and
a PATCH through the default public runner, precedence and CLI rollback. Scoped
Ruff and diff whitespace checks passed. Full Python regression in explicit CLI
rollback mode: 1232 passed, six failed. Four are the previously recorded failures;
two additional simulation tests encounter shared cache state (existing executable
skips install logging; tokenizer directory already exists). This run does not
claim full-suite default-SDK coverage: legacy tests mock CLI processes without
isolating SDK network access. No live cluster write or service restart performed.

Reuse report `.cache/reuse/kubernetes-default-report.json` has zero structural
errors and the same 94 candidate IDs as the previously analyzed expansion report
(only monitoring-operation source locations changed). Existing unrelated DB type
ownership decision still leaves global status WAITING_FOR_USER.
