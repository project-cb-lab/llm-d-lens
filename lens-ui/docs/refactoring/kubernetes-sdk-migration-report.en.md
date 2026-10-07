# Kubernetes SDK Optimization and Development Conventions

## 1. Optimization goals and current status

Migrate operations that are suitable for directly calling the Kubernetes API from external `kubectl` processes to the official async Python client, while preserving existing business validation, call results, and necessary CLI behavior.

**Current status: when neither backend configuration variable is set, migrated read/write operations now use the SDK by default.** The coexistence of SDK and CLI does not mean kubectl has been completely removed from the repository, nor does it mean all real cluster write operations have been validated.

| Item | Current implementation |
| --- | --- |
| Official dependency | `kubernetes==36.0.3` in [pyproject.toml](../../pyproject.toml), using `kubernetes.aio` |
| Data format | Transfer raw Kubernetes JSON through the official SDK to avoid losing dynamic resource fields by depending on fixed models; this does not convert all business objects into SDK model classes |
| Default path | Supported operations use the SDK; unsupported command forms select the CLI before execution |
| Rollback method | Explicitly set `PRISM_KUBERNETES_BACKEND=cli` |
| Failure behavior | If an SDK request fails, it directly returns the corresponding failure or exception and does not retry write operations via the CLI |
| Runtime environment | This round did not restart services or execute real cluster writes; existing processes must reload code before using the new default |

For specific configuration and supported scope, see the [Kubernetes operations document](../kubernetes-read-backend.md); for implementation records, see the [expansion plan](../../specs/changes/kubernetes-sdk-expansion.md).

## 2. Pre-optimization implementation approach and issues

| Original implementation | Possible issue or cost | Handling in this round |
| --- | --- | --- |
| Even ordinary resource queries launched kubectl | Depends on the executable, PATH, and client version; repeatedly creates processes and makes it hard to reuse client connections across calls | Covered requests now use the SDK directly, while preserving necessary tool dependencies |
| Callers consumed return code, stdout, and stderr | API status had to be converted into command results and then reinterpreted by callers; business logic was coupled to CLI output conventions | Provides structured API entry points while preserving the old result-adapter layer |
| `list_resources` returned an empty list for non-zero exit codes and invalid JSON | “No resources” and “query failed” might be indistinguishable, hurting diagnostics | Strict APIs preserve exceptions; for compatibility, the old list entry still preserves empty-list semantics and is not a comprehensive fix |
| Some cluster configurations with missing paths might inherit ambient environment context | The explicitly selected cluster might differ from the actually accessed cluster | The SDK path strictly resolves the explicit cluster; if it cannot be resolved, it fails rather than borrowing the default cluster |
| Node and Python queried session clusters separately | Query transport, authentication, and configuration handling were split across two runtimes | Node’s selected-session planning queries now reuse Python session endpoints |
| Evaluation diagnostics relied on periodic polling | Event response was limited by polling intervals and repeatedly reread resources | SDK Watch wakes fault inspection while preserving periodic validation and log queries |
| Subprocess and long-connection lifecycles were scattered | Timeouts, cancellation, credential refresh, and resource release were hard to validate uniformly | Shared authentication, connection-pool, and Watch modules are now centrally managed with additional regression coverage |

The CLI itself is still suitable for Helm, Kustomize, and specific kubectl workflows. The optimization is based on operation semantics and maintainability, not on the idea that all CLI usage should be deleted.

## 3. Current shared modules and concrete implementation

### 3.1 Module responsibilities

| Shared module | File path | Main entry points | Implementation and boundary |
| --- | --- | --- | --- |
| Cluster scoping and compatibility entry points | [kubernetes.py](../../llm_d_bench/utils/kubernetes.py) | `kubeconfig_environment`, `scoped_runner`, `run_kubectl`, `KubernetesCommandRunner` | Reuses registry/session; chooses SDK or CLI while preserving original caller contracts |
| Limited command adapter | [kubernetes_commands.py](../../llm_d_bench/utils/kubernetes_commands.py) | `execute_sdk_command`, `sdk_enabled`, `sdk_writes_enabled` | Executes only after recognizing complete argv forms; unknown parameters or combinations return the CLI path before execution and do not interpret arbitrary shell scripts |
| Raw APIs and resource discovery | [kubernetes_api.py](../../llm_d_bench/utils/kubernetes_api.py) | `api_session`, `query_resource`, `resolve_resource`, `server_version` | Built-in resource mappings, dynamic API discovery, namespaces and selectors, pagination; pagination failure does not return partial success |
| Old list-contract adapter | [kubernetes_reads.py](../../llm_d_bench/utils/kubernetes_reads.py) | `list_sdk_resources` | Preserves the existing caller behavior of returning an empty list on request failure and records redacted error categories |
| Resource write operations | [kubernetes_mutations.py](../../llm_d_bench/utils/kubernetes_mutations.py) | `create_sdk_resource`, `patch_sdk_resource`, `label_sdk_resource`, `scale_sdk_resource`, `delete_sdk_resource` | Explicit patch types, label conflicts, scaling, and delete waiting; determines whether the original resource has already been deleted based on UID |
| Authentication and credential cleanup | [kubernetes_auth.py](../../llm_d_bench/utils/kubernetes_auth.py) | `load_configuration`, `cleanup_configuration` | kubeconfig merge, TLS, token, ExecCredential; plugins use argument arrays, validate output, and handle timeout and cancellation |
| HTTP client reuse | [kubernetes_pool.py](../../llm_d_bench/utils/kubernetes_pool.py) | `start_pool`, `pooled_client`, `close_pool` | Reused by credential/TLS identity within the application lifecycle, with capacity and idle-cleanup boundaries; closed per request outside that lifecycle |
| Resource event watching | [kubernetes_watch.py](../../llm_d_bench/utils/kubernetes_watch.py) | `watch_resource_events` | Initial list, resourceVersion, BOOKMARK, 410 relist, bounded backoff, and cancellation; dedicated connections avoid exhausting the normal request pool |
| Node session-query bridge | [sdk_discovery.py](../../llm_d_bench/cluster/sdk_discovery.py), [planningDiscovery.ts](../../server/planningDiscovery.ts) | Session planning query endpoint, `queryCluster` | Active-session validation and a four-query whitelist; does not accept arbitrary client kubeconfig paths |

`run_kubectl` keeps its original name for caller compatibility, **but that name does not mean kubectl will be launched every time**. Commands accepted by the SDK adapter are translated directly into API requests.

### 3.2 Integration scope

| Caller | Integrated content | Preserved domain rules |
| --- | --- | --- |
| Shared runner calls in Storage, Model Cache, Simulation, and Monitoring | Recognized resource reads/writes now use the SDK | Resource lifecycle, state interpretation, and business validation in each module |
| [Deployment runtime](../../llm_d_bench/deploy/runtime/composition.py) | Supported validated commands enter the adapter | Namespace scope, allowed operations, output redaction, and execution evidence |
| [Cluster monitoring service](../../llm_d_bench/monitoring/cluster_stack/service.py) | SDK mode reuses the shared scoped runner | Monitoring installation, uninstall, and state aggregation |
| [Evaluation router](../../llm_d_bench/evaluate/router.py) | Cluster JSON / raw query adaptation | Original upper-layer failure-return contract |
| [Evaluation diagnostics](../../llm_d_bench/evaluate/harness_watch.py) | Watch triggers Pod fault checks | Periodic Pod/log validation and task cancellation |
| Node cluster planning | Version, node, DeviceClass, and ResourceSlice queries for the selected session now reuse Python | Default-context queries on the Node host still use the local CLI |

### 3.3 Supported scope and the boundary for retaining the CLI

| Operation | Current handling |
| --- | --- |
| Resource reads | Supported named/list operations, JSON/YAML/name output, label/field selectors, pagination, multi-resource queries, and some raw GET |
| Resource creation | namespace, and supported single core/apps/batch objects created from stdin |
| Modification and deletion | Supported patch, label, scale, cordon/uncordon, named or selector deletion, and delete waiting |
| Pod logs | Bounded reads for a specified Pod, supporting some container, tail, previous, and timestamps parameters |
| apply, Helm, Kustomize | Keep the existing CLI; do not silently turn client-side apply into server-side apply |
| exec, port-forward, SSH | Keep the original process/remote-execution approach; no assumption that the local SDK can replace remote network environments |
| Unsupported rollout/wait, streaming logs, advanced parameters | Select the CLI before execution; do not force conversion to the SDK just because the command name matches |
| Legacy `auth-provider` | Select the CLI before executing resource operations; ExecCredential plugins are supported by the SDK authentication wrapper |

The above is a summary; the full boundary is governed by the [operations document](../kubernetes-read-backend.md) and the adapter source code. Tools such as kubectl still need to be retained; using the official library also does not mean authentication plugins never spawn subprocesses.

## 4. Benefits and limitations

| Dimension | Capability gained | Limitation |
| --- | --- | --- |
| Processes and connections | Covered requests no longer start kubectl, and ordinary HTTP clients can be reused | No performance benchmark was executed, so concrete latency or throughput improvements cannot be given |
| Maintenance and extension | Authentication, pagination, resource discovery, write operations, and watching now each have clear maintenance entry points | The limited argv adapter still requires maintenance and cannot claim to implement full kubectl semantics |
| Error handling | Strict APIs preserve exceptions, and failed write requests are not automatically replayed | The compatibility layer still exposes different contracts such as empty list, None, and exit code; callers must choose the appropriate entry point |
| Cluster isolation | Explicit cluster resolution is stricter, and clients are isolated by identity | Default contexts and remote CLI still have their own configuration sources, which must be checked according to the actual call path |
| Lifecycle | Tests cover plugin process-group cleanup, client close, and Watch reconnect credential refresh | Long connections, auth providers, and all CRD combinations have not been comprehensively validated on real clusters |
| Integration cost | Preserves old caller contracts, reducing the scope of one-time business-module rewrites | This cannot be used to promise zero impact; the SDK default change still requires post-deployment observation |

## 5. Configuration, rollback, and development conventions

### 5.1 Configuration precedence

| Full switch `PRISM_KUBERNETES_BACKEND` | Old switch `PRISM_KUBERNETES_READ_BACKEND` | Result |
| --- | --- | --- |
| Unset | Unset | Supported reads/writes default to the SDK |
| `sdk` | Any value | The full switch takes precedence, and supported reads/writes use the SDK |
| `cli` | Any value | The full switch takes precedence, rolling back to the CLI |
| Unset | `sdk` | Keeps the old behavior: SDK for reads, CLI for writes |
| Unset | `cli` | CLI |

Invalid values in the effective configuration raise errors. Running services must reload configuration; SDK mode still uses some CLI paths according to the boundary above.

### 5.2 Rules that future development must follow

| Convention | Requirement | Maintenance location |
| --- | --- | --- |
| Reuse first | Check the capability index, real callers, and tests; for new structured operations, prefer reusing API/write-operation modules, and use the shared runner for old command-result contracts | [workflow skill](../../.agents/skills/workflow/SKILL.md), [capability index](../reuse-map.md) |
| Do not reimplement transport | Do not scatter new kubectl subprocesses, shell strings, or another set of SDK initialization for already covered capabilities; reuse identity, authentication, connection, and timeout management | [backend skill](../../.agents/skills/backend/SKILL.md#kubernetes-operations) |
| Preserve business validation | Namespace, permission scope, and deployment-allowed-operation validation must happen before SDK/CLI execution; the shared tool layer must not take over business rules | [deployment skill](../../.agents/skills/deployment/SKILL.md#kubernetes-transport-and-compatibility) |
| Do not replay failed writes | An SDK failure must not automatically retry through the CLI; choose the CLI only when the unsupported form is confirmed before execution | Command adapter and operations document |
| Do not swap apply semantics silently | Do not directly replace client-side apply with server-side apply; field ownership, conflicts, and compatibility impact must be made explicit | deployment skill |
| Interpret errors correctly | New business logic that needs to distinguish “no resource” from “request failed” must use the strict API; do not treat the empty array from the old list entry as proof of success | backend skill |
| Isolate tests | Use local API fixtures or SDK-boundary mocks; mocking only subprocess cannot isolate the default SDK path. Explicitly select the CLI for CLI-specific tests, and cover SDK paths separately | backend skill, [integration tests](../../llm_d_bench/utils/test_kubernetes_integration.py) |
| Manage lifecycle | Close async iterators when Watch consumers exit early; reuse existing session/pool cleanup to avoid leaking responses, connections, and temporary certificates | Operations document |
| Registration and naming | Python utilities belong in `llm_d_bench/utils/` and use `snake_case.py`; domain rules remain in domain directories; register new shared capabilities in the catalog and generate the index | [Shared file conventions](shared-code-guide.md), workflow skill |

These rules have been integrated into the backend and deployment skills, and agents should read them when modifying related code; skills are development guidance, do not participate in runtime backend selection, and cannot replace tests or code review.

## 6. Verification records and delivery boundary

The table below records results captured during this implementation round, **not tests re-executed while writing this report, and it does not guarantee coverage of later parallel changes**.

| Verification | Recorded result | Scope it can prove |
| --- | --- | --- |
| Latest Kubernetes-focused Python tests | **163 passed** | Local real SDK HTTP/TLS, authentication, pagination, CRUD, Watch, compatibility entry points, plus default SDK with no configuration and explicit rollback |
| Node planning tests | **3 passed** | Session-query bridge, concurrent queries, and failure-result handling |
| TypeScript type check and targeted lint | Passed | Type and static checks at execution time, not equivalent to full-repository lint all green |
| Latest full Python regression | **1232 passed, 6 failed**, in explicit CLI rollback mode | Validates rollback and old callers; cannot claim the full test suite passes in default SDK mode |
| Full failure set | 4 pre-existing items: endpoint hostname expectation, capacity expectation, and two workload output expectations; 2 more involve the simulation executable and tokenizer shared-cache state | Failures were not removed by weakening tests or modifying unrelated business rules |
| Real cluster comparison | SDK/kubectl server versions match, and the name/UID sets of 5 nodes match | Read-only verification only; does not prove real writes, all auth providers, or CRD compatibility |
| Reuse checks | 0 structural errors, 94 candidate groups analyzed; global status `WAITING_FOR_USER` | The existing pending DB type-ownership issue remains, so it cannot be claimed that reuse checks passed completely |
| Skill validation | backend/deployment format, local links, and anchor checks passed | The guidance structure is valid, but it is not a guarantee that future agents will necessarily follow it |

This round did not execute real cluster writes, deployment rollback, or service restart. Future rollout still requires target-environment validation of authentication, RBAC, representative write operations, and retained CLI paths; those unvalidated items cannot be substituted by the official status of the SDK library.
