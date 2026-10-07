# Unified storage reuse audit

Audit date: 2026-09-18. Scope: all **93** candidates in `.cache/reuse/storage-report.json`, including candidates 31–93 omitted by the console preview. Base: `c708b9ee4c642ecd2040798acc693e2b9bdc82b8`. Report SHA-256: `b082aee7f3ffe13d0eb41dafbfd7f64acf0263a1ef7aff672d882c475f944663`.

The report scanned **386 files** and counted **109 changed files** in a workspace that already contained unrelated edits. Every candidate's two source locations was inspected; relevant storage callers and surrounding implementations were also read. Candidate numbers below are one-based positions in this report snapshot. Line numbers can move as ongoing fixes land; decisions refer to the implementations, not a similarity score.

**Classification:** 8 storage-relevant candidates, 15 incidental provider/backend/import candidates, and 70 pre-existing frontend candidates. A changed filename alone does not establish that the matched code was added by the storage work. For example, the SimulationDashboard diff contains earlier HTTP/formatting reuse, while the matched state/form/table code predates this task.

| Group | Candidate numbers | Decision |
| --- | --- | --- |
| Storage persistence | 6, 7, 8, 18, 19, 20 | Domain JSON stores share paths; keep credential permissions, alias serialization, model validation, directory shape, locks and domain-specific errors in their existing stores. |
| Monitoring evidence | 9 | Both operation stores call register_artifacts for terminal diagnostic snapshots. Accelerator post-install/argv and cluster-stack script/verifier/recovery models remain domain-owned. |
| Execution evidence | 16 | Host and pod execution share RunContext, CommandResult and process helpers. Pod staging, periodic/final collection and cleanup differ from local subprocess execution; keep lifecycle separate. |
| Provider diagnostics/rendering | 1, 2, 3, 4, 5 | Existing adapter diagnostics, discover/restore hooks and mount/build setup remain provider-owned; resolve_mount is already shared. Storage changes do not justify merging providers. |
| Backend incidental overlap | 10, 11, 12, 13, 14, 15, 17 | Domain errors, imports, mapping expressions, optional Pydantic fields and keyword signatures are not a shared persistence contract. Simulation analytics/process models are already reused where applicable. |
| Cross-layer imports | 21, 28, 50 | Node guidePlanning imports and React component imports have normalized-token overlap only. They cannot be unified across server/browser dependency boundaries. |
| Prior UI state | 22, 23, 30, 33, 34, 35, 36, 37, 43, 44, 45, 46, 53, 54, 55, 56, 58, 63, 64, 65, 66, 67, 68, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 86 | Existing useState declarations/form defaults represent distinct domain state; no storage behavior added in these spans. |
| Prior modal composition | 26, 31, 32, 40, 41, 42, 47, 87, 89, 90 | Existing useSubmission, FormError, Modal and Button composition; delete namespace/file confirmation and edit/download actions stay with their domains. |
| Prior UI imports | 27, 48, 49, 91, 92 | Existing shared component/hook imports demonstrate reuse; no new abstraction required. |
| Prior monitoring links | 24, 29, 59, 60, 61 | Similar dashboard-link lookup/error handling is outside artifact storage; accelerator and stack endpoints and namespaces remain distinct. |
| Prior UI presentation | 25, 38, 51, 52, 57, 62, 83, 84, 85, 88, 93 | Existing tabs, filters, cards, labels, row-size controls and status presentation; some real duplication remains, but is unrelated to storage. Node drift and model sync also have different domain meaning. |
| Prior configuration editing | 39 | Both existing callers already use saveConfiguration; selection replacement differs between the dashboard and wizard. Immutable artifact publishing is implemented beneath that API. |
| Prior guide selection | 69 | Existing guide/accelerator lookup in distinct page/workflow state; unrelated to storage roots or artifact registration. |

Storage decisions are grounded in these contracts:

- [AI provider store](../../llm_d_bench/ai_providers/store.py), [deployment store](../../llm_d_bench/deploy/run_store.py), [model cache store](../../llm_d_bench/model_cache/store.py) and [volume store](../../llm_d_bench/storage/store.py): reuse [storage_path](../../llm_d_bench/utils/paths.py). AI provider writes retain mode 0600 and alias serialization; deployments preserve execution metadata and run/case ownership; cache entries and volumes retain separate models, subdirectories and conflict errors. The earlier withdrawal of a generic JSON store is documented in [the refactoring history](README.md). This task does not reintroduce it or decide the DB PR's schema ownership.
- [Accelerator operations](../../llm_d_bench/monitoring/accelerator/operations.py) and [cluster-stack operations](../../llm_d_bench/monitoring/cluster_stack/operations.py): reuse the same manifest function for snapshot integrity/retention metadata, while each manager retains request validation, locking, recovery, verification and log policies. Similar snapshot-writing lines are adapter code around this shared primitive, not justification for merging lifecycle managers.
- [Host execution](../../llm_d_bench/simulation/process.py), [pod execution](../../llm_d_bench/simulation/incluster.py) and [simulation service](../../llm_d_bench/simulation/service.py): reuse result/context contracts and [register_artifacts](../../llm_d_bench/utils/artifact_store.py). Pod staging/final collection/deletion and host child termination remain different operations. Interrupted output is marked incomplete; registration errors do not publish artifact references. These behaviors are covered by [simulation tests](../../tests/test_simulation.py).
- [Configuration publishing](../../llm_d_bench/configuration/service.py) and [deployment evidence](../../llm_d_bench/deploy/service.py) also consume the shared artifact primitives. Their immutable configuration identity and deployment state transitions remain domain-owned. No candidate requires making shared storage depend on pages or merging JSON, streaming, task and deployment error contracts.
- [Node guide planning](../../server/guidePlanning.ts) uses [storagePath](../../server/storagePaths.ts) for its disposable guide cache. Its matches against React imports are incidental and cannot establish a cross-layer reusable operation.

The existing report status is **WAITING_FOR_USER**, with **zero errors** and **no stale decision IDs**. Its sole pending item is `ca783e76-c2cd-4880-b745-0f73ee1e5ff2`, the earlier keep/delete ownership question for `PageInfo`, `TaskRef` and `TaskStatus` in `llm_d_bench/schemas/common.py` and `llm_d_bench/tasks/models.py`. This audit neither resolves that decision nor edits those files, suppresses candidates, changes policy, or records approval. The report must not be described as passing.

No additional unresolved storage-contract decision was found in these 93 candidates. This is a candidate audit, not a new test run or proof that every repository capability is duplication-free. Re-run the normal reuse check after final changes; this document records only the identified report snapshot.

## Final check

The final report `.cache/reuse/storage-report-final.json` again contains 93 candidates with the same source-path pair set; no new candidate pair was introduced by final fixes. It has zero registration errors, one unchanged pending ownership decision, and status `WAITING_FOR_USER`. SHA-256: `7d6beb5273bab406a5d3c4c34f7eb99185e61c6bdef5a15d07530d1225c77566`.


## Removal of legacy directory overrides

The user explicitly requested that new writes use the new paths, without legacy directory variables taking precedence. The shared Python/Node path contracts and their existing catalog entries now reflect that decision; no new capability was introduced.

The report `.cache/reuse/new-paths-report.json` contains 94 candidates. Comparison of every source-path pair against the prior final report found one additional pair: `evaluate/router.py:883` and `utils/kubernetes_commands.py:25`. These are respectively a benchmark metric-to-unit dictionary and a Kubernetes CLI flag-to-option dictionary. Their normalized literal syntax overlaps, but they have separate domain meanings and callers; no shared storage implementation is missing. All other path pairs retain the decisions above.

The report has zero errors and no stale decisions. Its status remains `WAITING_FOR_USER` solely for the unchanged PageInfo/TaskRef/TaskStatus ownership decision. This storage change does not resolve that unrelated question or bypass startup checks.

Report SHA-256: `2dd62b3135a4d3f26032fa0bde3ab192214dedc7d33be7edb8b88e57dada55d5`.


## Withdrawal of lifecycle expansion

At the user's request, the additional conversation/audio archive, scheduled expiration,
dataset content-version layout and shared commit checkout implementation were removed.
Unified storage roots and existing artifact inventories remain. No new reusable capability
was retained, so no additional catalog entry is needed.

`.cache/reuse/storage-withdrawal-report.json` contains the same 94 source-path candidate
pairs reviewed above, zero errors and no stale decisions. The lifecycle-policy request
was resolved as withdrawn using the user's explicit instruction. The unrelated schema
ownership request remains pending; the reuse check is not reported as passing.
