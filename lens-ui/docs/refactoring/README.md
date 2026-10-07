# Extracting Frontend and Backend Shared Capabilities: Refactoring Notes

> [Code Quality, Module Coupling, and API Integration Assessment Report](code-quality-report.md) contains the current quality metrics, module responsibilities, shared capability reuse, 166 HTTP operations, and implementation acceptance criteria.


> The [assessment report](code-quality-report.md) summarizes similar implementations by page, 10 page tasks, the purpose and disposition of 166 APIs, and the locations of API definitions and shared capabilities. The scan scope and snapshot are governed by Section 1 of that report.


[Shared Component Inventory and File Conventions](shared-code-guide.md): extracted capabilities, file paths, and naming conventions.

[Kubernetes SDK Migration Report and Development Conventions](kubernetes-sdk-migration-report.md): before/after migration approach, shared capabilities, CLI retention boundaries, benefits, conventions, and verification records.

See [Nine Shared Capabilities Integration Record](nine-shared-capabilities.md) for the specific integration and verification details of the nine shared capabilities in this round.


> **Overall assessment and next-step plan:** [Code Quality and API Integration Assessment Report](code-quality-report.md) — lists issues by module, integration points, API conventions, and explains the purpose and necessity of all 166 APIs one by one. This document continues to record the shared-capability extraction that has already been implemented.


Supporting process design: [Reuse-First AI Development Workflow: Architecture, Implementation, and Verification Plan](reuse-first-agent-design.md).

> 2026-09-18 scope adjustment: by user decision, the extraction of structured data file read/write was withdrawn, and the persistence architecture will be handled by the DB PR. This round keeps the frontend shared request layer, UI, and non-storage foundational definitions.

## The five-item list for this round (current status)

| Item | Implemented work and boundaries |
| --- | --- |
| Unified ordinary JSON requests | In-page Agentic, Cluster, Evaluation, Simulation, Bootstrap, Model Market, and standalone clients now share unified request transport; Evaluation/Simulation/model probe keep specialized error interpretation, and strict JSON success parsing can be selected explicitly. FormData does not set JSON Content-Type. |
| Page async state | The four resource pages AI Provider, Storage, Model Cache, and Deployment share AsyncState; each page keeps its own loading/empty/error copy and actions, as well as its data retention strategy during refresh. Multi-resource composite pages are not forcibly wrapped in a page-wide state. |
| Shared delete and submit interactions | useSubmission/createSubmissionGuard is used for four delete dialogs, AI Provider create/edit, and Deployment edit; duplicate-prevention synchronization, failure unlock, and success lock are configurable. Nine dialogs reuse FormError. Confirmation phrases, retain-file options, and similar items remain in the domain layer. |
| Table, formatting, and chart foundations | Three resource lists reuse PaginationControls; four domains reuse errorMessage, and Deployment/Storage share time formatting. The existing ChartTooltip/ChartTooltipRow is already reused and registered, so no separate chart library is added; data aggregation and business filtering are not merged. |
| Invalid wrappers and idle definitions | The StatusBadge wrapper and the unused workflowModel were removed. Ownership of PageInfo/TaskRef/TaskStatus relative to the DB PR is still pending user decision, so they are not removed for now. |

In addition, duplicated software-download polling in create/edit Cluster has been moved into clusterBackend; duplicated configuration SHA-256 validation across the two Evaluation pages has been moved into the domain checksum module. All new capabilities have been registered in the reuse catalog.

### List of places that keep raw fetch

| Location | Reason for keeping it |
| --- | --- |
| Shared httpClient | The only ordinary JSON transport implementation |
| download.js | Binary download |
| EvaluationDashboard / EvaluationTaskWizard manifest | YAML text response |
| Playground chat | SSE stream, read event by event |
| remoteDeployBackend delete | Accepts only 204; other success statuses must also be rejected; shared error parsing |
| Bootstrap cancel / CreateClusterWizard clear draft | Best-effort send, does not consume the response; draft clearing uses keepalive |

The fetch appearing in mockBackend is only in comments, not a call pending migration.

### Current verification results

- All `src` frontend tests: **236 passed**.
- Production build: passed (13.81s), still reports a large bundle.
- Whole-repository ESLint: **29 errors / 10 warnings**; it cannot be claimed fully green, and this round did not change check rules.
- Local documentation links and `git diff --check`: passed.
- Real browser interactions and external cluster operations: not executed.

### Reuse checks and pause boundaries

The checks include the previously approved storage rollback, so the Store similarity items in the report are intentionally restored. Other candidates include import/useState syntax, domain layouts that still call the existing Modal/Button/Select, and different resource states and monitoring lifecycles; these are not all merged indiscriminately. The same error display, submit locking, pagination, polling, and checksum logic have already been extracted.

Pending record `ca783e76-c2cd-4880-b745-0f73ee1e5ff2` concerns only DB type ownership. No new ignore rules were added, and pending was not treated as approved. The reuse check status WAITING_FOR_USER cannot be written as fully passed.

## 1. Background and goals

Previously, frontend clients repeatedly implemented `fetch → JSON parsing → error conversion`; multiple backend Stores repeatedly implemented `file read → JSON parse → model validation`, as well as temporary file write-and-replace logic.

The goal of this refactoring is: **use one implementation for the same technical capability, while keeping business rules in domain modules.**

| Included in scope | Kept in the domain layer |
| --- | --- |
| JSON HTTP requests, response parsing, common error fields | Routes, parameters, list-structure validation, business error judgment |
| Monitoring request authentication headers | Installation, polling, and lifecycle of each monitoring component |
| Foundations for loading, failure, empty-data display, etc. | Request lifecycle, cancellation, refresh, business operations |

## 2. Core changes: integrated shared capabilities

| Capability | Before refactoring | After refactoring | Shared entry point |
| --- | --- | --- | --- |
| JSON requests | Each client called fetch, read responses, and checked status on its own | Reuse `requestJson`, `postJson`, `readJson` | [httpClient.js](../../src/api/httpClient.js) |
| HTTP errors | Each client repeatedly extracted error text and status code | `problemError` consistently keeps status, code, title, details, retryable, and body | [httpClient.js](../../src/api/httpClient.js) |
| Monitoring authentication | Three clients each read tokens and assembled auth headers | `requestMonitoringJson` uniformly sets auth headers and fallback error-code handling | [monitoringClient.js](../../src/api/monitoringClient.js) |

### 2.1 Frontend integration list

| Module | Client | Integration method |
| --- | --- | --- |
| AI Provider | [aiProvidersBackend.js](../../src/components/AIProviders/aiProvidersBackend.js) | Shared JSON requests |
| Storage | [storageManagementBackend.js](../../src/components/StorageManagement/storageManagementBackend.js) | Shared JSON requests, while keeping query-parameter and response-structure validation |
| Model Cache | [modelCacheBackend.js](../../src/components/ModelCache/modelCacheBackend.js) | Shared JSON requests, while keeping download, sync, and log interfaces |
| Cluster | [clusterBackend.js](../../src/components/OptimizationWorkspace/clusterBackend.js) | Shared JSON requests |
| Candidate | [candidateBackend.js](../../src/components/OptimizationWorkspace/candidateBackend.js) | Shared POST requests |
| Configuration | [configurationBackend.js](../../src/components/OptimizationWorkspace/configurationBackend.js) | Shared GET/POST/DELETE requests; keep resource caching and array fallback |
| Cluster Monitoring | [clusterMonitoringStackBackend.js](../../src/components/ClusterMonitoringStack/clusterMonitoringStackBackend.js) | Shared monitoring-authenticated requests |
| Accelerator | [acceleratorObservabilityBackend.js](../../src/components/ClusterMonitoringStack/acceleratorObservabilityBackend.js) | Shared monitoring-authenticated requests |
| GPU Driver | [gpuDriverBackend.js](../../src/components/ClusterMonitoringStack/gpuDriverBackend.js) | Shared monitoring-authenticated requests |

### 2.2 Withdrawn storage extraction

To avoid overlapping with the DB PR, the following files were restored to their implementation from before refactoring commit `3137a04`. This rollback restores code only; it does not migrate or delete runtime data.

| Store | Rollback result |
| --- | --- |
| [StorageVolumeStore](../../llm_d_bench/storage/store.py) | Restored local JSON read/write and instance lock |
| [ModelCacheStore](../../llm_d_bench/model_cache/store.py) | Restored local JSON read/write and instance lock |
| [AIProviderStore](../../llm_d_bench/ai_providers/store.py) | Restored local JSON read/write and instance lock |
| [JsonDeploymentRunStore](../../llm_d_bench/deploy/run_store.py) | Restored its own model-read and atomic-write methods |
| [JsonBenchmarkRecordStore](../../llm_d_bench/agentic/benchmark_store.py) | Restored the original versioned record-file read/write; no longer uses shared file storage |

The shared `llm_d_bench/common/json_store.py` has been removed, and the `json-file-store` entry was removed from the capability catalog accordingly. This is not a declaration that the DB migration is complete; the project still uses the original file storage, to be taken over later by the DB PR.

### 2.3 Frontend integration additions (2026-09-18)

| Work in this round | Actual result |
| --- | --- |
| Remote Deployment / Guide Planning | Uses the existing shared HTTP layer; caching, session binding, auth body, and the 204 delete contract remain unchanged |
| Playground | Cluster queries and session creation reuse the Cluster client; SSE remains unchanged |
| AsyncState | Expanded with loadingContent/errorContent/emptyContent; Storage and Model Cache keep their original copy and actions and are integrated |
| AI Provider | Restored the add button for the empty state, removed an unreachable children branch, kept the list on refresh failure, and avoids duplicate error display |
| StatusBadge | Had no actual callers and only forwarded StatusChip, so the file and exports were removed; the domain PodStatusBadge remains |

The reuse report contained 30 candidate groups, all reviewed one by one: 6 groups were approved Store rollbacks; the rest were mainly similarities in import/useState syntax and do not represent the same business logic. There were also two historical page search areas and cluster-selection areas with similar appearance that still reuse existing foundational components such as Input/Select; layout and domain filtering flows were not modified in this round. No CI exceptions were added, and no report content was hidden.

## 3. Foundational definitions that are only partially landed

The following already exist, but should not be treated as equivalent to “callers have been fully unified.”

| Capability | Current status | Remaining work |
| --- | --- | --- |
| [AsyncState](../../src/components/shared/AsyncState.jsx) | Integrated in AI Provider, Storage, and Model Cache; display order is loading → error → empty → children, and it supports domain display slots | Browser interaction regression |
| [DomainError](../../llm_d_bench/core/exceptions.py) | Defines common exceptions plus 404/409/422 subclasses; [API handlers](../../llm_d_bench/api/problems.py) have been registered | Domain exceptions have not yet been migrated comprehensively |
| [PageInfo / TaskRef](../../llm_d_bench/schemas/common.py) | Shared types are defined | Not yet widely integrated into interfaces |
| [TaskStatus](../../llm_d_bench/tasks/models.py) | Defines queued/running/succeeded/failed/cancelled | Existing task executors and state machines are not unified |

## 4. Compatibility boundaries and remaining items

| Item | Current boundary / remaining work |
| --- | --- |
| JSON requests | For 204 responses, callers receive their configured fallback; if JSON parsing fails, callers also receive the fallback, which defaults to an empty object. This is not a strict JSON validator. |
| Authentication | GitHub token injection exists only in the monitoring adapter layer and is not automatically added to all JSON requests. |
| AsyncState usage | emptyContent carries empty-state actions; on AI Provider refresh failure, previously loaded data is kept, while other pages keep their original error strategies. |
| Specialized requests | Remote Deployment and Guide ordinary JSON requests are integrated; strict 204 delete checks and reconnect fallback are kept. Playground cluster queries reuse the cluster client, while SSE keeps its specialized implementation. Evaluation field-validation error formats remain in the domain layer. |
| Tables and Modal | No comprehensive merge was performed; only foundational parts with confirmed semantic equivalence should be reused. |

## 5. Verification records

### Verification for this round of frontend integration (2026-09-18)

- HTTP, deployment client, Guide, Playground, AsyncState, and resource-cache tests: **16 passed**.
- `npm run build`: passed; bundle-size warning remains.
- Targeted ESLint for the frontend files changed in this round: passed.
- Whole-repository `npm run lint`: 32 errors and 10 warnings, all in files unchanged in this round; rules were not changed and errors were not suppressed.
- Reuse checks: 30 candidate groups analyzed, with no catalog errors and no pending items; see 2.3.
- Local documentation links and `git diff --check`: passed. Real browser interaction, real deployment, and external service calls were not executed.

### Verification for this storage rollback (2026-09-18)

- The five Stores and their corresponding files in `3137a04^` are byte-for-byte identical; no residual shared-storage imports remain.
- Store, service, and API caller regression: **183 passed**, including top-level Deploy tests; does not include full runtime tests.
- `reuse:check --base c708b9ee4c642ecd2040798acc693e2b9bdc82b8`: no errors and no pending items; the report contains 6 duplicate candidate groups.
- Candidate analysis: 1 group is AI Provider/Deployment read-write, 1 group is AI Provider/Model Cache initialization and creation, 2 groups are Deployment and Model Cache/Storage creation flows, and 2 groups are resource Store imports and class declarations. All of these are original implementations the user explicitly requested to restore, to be taken over later by the DB PR, so they are not re-extracted and no check exceptions are added.
- The `json-file-store` capability entry has been removed and the capability index regenerated; this round withdraws a capability and does not add a new catalog entry.
- Local link checks for the README and capability index passed.

### Historical refactoring records

The table below records results from earlier development, **not results re-executed for this documentation task, and it does not guarantee coverage of all current baseline changes**.

| Check | Historical result | Coverage boundary |
| --- | --- | --- |
| Three resource Store/service-related tests | 32 passed in the most recent cleanup round | Storage, Model Cache, AI Provider |
| The above tests plus Benchmark Store | 33 passed in an earlier round | Includes Agentic Benchmark Store |
| Top-level Deploy tests and Benchmark Store | 93 passed | Not equivalent to complete Deploy runtime tests |
| Complete Deploy directory and Benchmark Store | 163 passed, 1 failed | The endpoint smoke test URL was inconsistent with runtime expectations; this documentation task did not verify whether it is a baseline issue |
| ESLint | Historically reported as passed; some commands were recorded only in startup output | Not current complete verification evidence |
| Production build / page regression | No complete pass record confirmed for this task yet | Cannot claim full validation completed |

Subsequent changes may validate according to actual impact scope:

```bash
npm run build
npm run lint -- --no-warn-ignored
./.venv/bin/pytest -q \
  llm_d_bench/storage/test_store.py \
  llm_d_bench/model_cache/test_store.py \
  llm_d_bench/ai_providers/test_service.py \
  llm_d_bench/agentic/test_benchmark_store.py
```

## 6. How to reuse these in future development

1. Locate the entry from the [capability index](../reuse-map.md), then read the source, callers, and tests.
2. For ordinary JSON requests, prefer reusing the HTTP layer; use domain adapters for authentication strategy.
3. Storage and reading of structured data will be designed uniformly by the DB PR; this round does not reintroduce a shared JSON file storage layer.
4. When a shared contract changes, validate existing callers, update `.reuse/catalog.json`, and regenerate the capability index.
5. Check changes according to the [reuse workflow](../../.agents/skills/workflow/SKILL.md); when business semantics are unclear, confirm first rather than using code similarity as a substitute for a decision.

Related entry points: [Project README](../../README.md) · [Reuse development notes](reuse-first-agent-design.md) · [Contribution Guide](../../CONTRIBUTING.md) · [Original refactoring PR #48](https://github.com/intel-sandbox/llm-d-prism/pull/48)
