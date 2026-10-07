# Kubernetes SDK read migration

Historical first-stage record. Current expanded scope and validation are in
[Kubernetes SDK expansion](kubernetes-sdk-expansion.md).

## Scope and compatibility

Implement the approved hybrid approach: Python resource reads may use the official
Kubernetes client; Helm, Kustomize, SSH, writes, and port-forward keep their existing
execution paths. First delivery extends `utils.kubernetes.list_resources` only.
Default backend is `cli`. `PRISM_KUBERNETES_READ_BACKEND=sdk` opts supported resource
lists into the SDK. Unsupported resources retain CLI routing before execution;
SDK request failures never trigger a CLI retry.

Pinned SDK authentication inspection found that exec-plugin failure logs stderr
and can silently continue, and cancellation leaves helper processes alive. Keep
selected users with `exec`/`auth-provider` on CLI, chosen from the merged config
before calling SDK authentication. Static token and certificate reads use SDK.

Preserve the public signature, raw Kubernetes JSON field names, namespace and label
selection, and the existing empty-list compatibility behavior on API failures.
Log a sanitized error category, never response bodies or credentials. SDK setup
errors must surface. Explicit cluster selection must resolve a real kubeconfig
before SDK access; do not change legacy CLI configuration semantics in this task.

Use independent client configurations, bounded requests, pagination, and explicit
client cleanup. Do not share mutable global SDK configuration. Start with core and
apps resource lists used by existing callers; custom resources remain on CLI.
Watch and write migration are subsequent stages, not part of this delivery.

## Implementation plan

**Goal:** Deliver an opt-in SDK read backend with a verified CLI rollback.

**Architecture:** Keep the existing public facade and CLI runner. Add a focused
SDK list adapter next to it; reuse cluster registry/session resolution.

**Tech stack:** Python 3.11+, official `kubernetes` client, pytest, local HTTP server.

**Spec:** This document; implements the hybrid design approved in this conversation.

- [x] Characterize current caller tests and record the baseline.
- [x] Write failing tests through `list_resources` using a local API server and
  temporary kubeconfigs. Cover routing, namespace, labels, pagination, raw fields,
  API failures, explicit cluster isolation, missing config, timeout, and rollback.
- [x] Add a pinned official SDK dependency and implement the adapter, preserving
  the public function contract and the default CLI route.
- [x] Run SDK contract tests and affected storage/monitoring/cluster regressions.
- [x] Register the public capability in `.reuse/catalog.json`, regenerate the map,
  and analyze `reuse:check` against `c708b9ee4c642ecd2040798acc693e2b9bdc82b8`.
- [x] Document activation, rollback, resource coverage, and validation limitations.

## Reuse decision

| Candidate | Decision |
| --- | --- |
| `utils.kubernetes.list_resources` | Extend in place; preserve callers and CLI behavior. |
| `kubeconfig_environment`, cluster registry/sessions | Reuse resolution; add SDK-only fail-closed selection. |
| `utils.shell` | Retain for CLI; not an HTTP client abstraction. |
| `RestrictedKubectlRunner` | Retain deployment policy; do not merge with read transport. |
| Node planning discovery | Defer service-boundary changes to a separate migration. |

Local semantic search plus source/caller inspection found no existing Kubernetes
SDK adapter. Existing HTTP helpers do not implement kubeconfig authentication or
Kubernetes resource contracts. No unrelated worktree changes belong to this task.

## Validation record

- Baseline focused storage/GPU-driver/path suite: 87 passed. Initial collection of
  all utils tests encountered an in-progress unrelated `validate_namespace` test;
  concurrent work subsequently supplied that function. Its changes were preserved.
- Final utils/storage/monitoring/model-cache/cluster and `tests` regression run:
  506 passed, four existing framework deprecation warnings.
- SDK tests use the real pinned client against a local HTTP fixture. No live
  cluster was accessed and no deployment or service restart was performed.
- Independent review found upstream dynamic-authentication defects. Both were
  addressed by routing selected exec/auth-provider users to CLI before SDK auth.
- New adapter and test files pass Ruff lint and format checks.
- Full reuse report: `.cache/reuse/kubernetes-sdk-report.json`; 80 candidate pairs,
  none involving this task's implementation files. Reviewed all pair locations:
  storage-repository overlap, monitoring-service overlap, and frontend component,
  state and presentation overlap belong to concurrent/pre-existing work; this
  read adapter does not change those contracts. No unrelated extraction performed.
- Reuse validation has zero structural errors but remains `WAITING_FOR_USER` for
  existing decision `ca783e76-c2cd-4880-b745-0f73ee1e5ff2` about unused DB types
  (`PageInfo`, `TaskRef`, `TaskStatus`). No approval or resolution was fabricated.

Activation and rollback: [Kubernetes read backend](../../docs/kubernetes-read-backend.md).
