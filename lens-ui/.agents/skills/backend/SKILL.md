---
name: backend
description: Use when changing Node or Express services in server/, Python APIs and domain services in llm_d_bench/, persistence, backend validation or task execution.
---

# Backend

Follow [workflow](../workflow/SKILL.md) for discovery,
registration and unresolved decisions. All source paths below are repo-relative.

## Locate the implementation

- `server/`: Node/Express endpoints, integrations and proxy boundaries. Inspect
  existing callers and `server/http.ts` before adding outbound JSON requests;
  compare error, timeout and streaming contracts before reuse.
- `llm_d_bench/`: Python APIs and domain services. Start in the owning domain
  (`configuration`, `deploy`, `evaluate`, `simulation`, etc.), then inspect
  `core/`, `schemas/`, `tasks/` and `utils/` for existing lower-level contracts.
- Use [the capability map](../../../docs/reuse-map.md) and current source to
  locate storage and Kubernetes helpers. A removed implementation or historical
  design document is not an available dependency.

## Kubernetes operations

Read the [SDK/CLI operating guide](../../../docs/kubernetes-read-backend.md)
when changing Kubernetes calls. Prefer the existing `kubernetes_api` and
`kubernetes_mutations` helpers for structured operations; use
`utils.kubernetes.run_kubectl` / `scoped_runner` when callers need the existing
command-result contract. Inspect their error contracts before choosing:
`list_resources` retains legacy empty-list behavior on request failures.

Reuse cluster/session identity resolution, authentication, client pooling and
timeout/cancellation handling. Do not add scattered kubectl subprocess calls,
shell-string construction or separate SDK initialization for covered operations.
For Node session discovery, reuse the existing Python session endpoint rather
than introducing another client or accepting arbitrary kubeconfig paths.

SDK request failures must not trigger CLI retries, especially for writes.
Unsupported command forms select CLI before execution; consult the operating
guide for retained CLI operations. Keep domain validation before transport.

Tests must isolate SDK network access with local API fixtures or mocks at the
SDK boundary. A subprocess mock alone no longer isolates default Kubernetes
calls. Explicitly select CLI for CLI-specific tests; cover SDK behavior separately
and do not globally disable SDK merely to make a suite pass. Existing examples
are in `llm_d_bench/utils/test_kubernetes_integration.py`.

## Hardware profiles

Hardware-specific behavior is data-driven. Declare it in a profile
(`llm_d_bench/hardware/profiles/<vendor>.json`, shape in
`llm_d_bench/hardware/schema.json`) and, only when code is required, a provider
(`llm_d_bench/hardware/providers/<vendor>.py`) contributing driver
install/manifests and access `modes`, device resources, monitoring images and
dashboards, telemetry `device_metric_sources`, and the default runtime image.
Surface it through `GET /api/v1/hardware/capabilities` (see
`llm_d_bench/hardware/`) and the cluster overview `hardware` summary.

Do not hardcode a vendor list, a device-metric name/query, a DCGM/GFD/device-plugin
manifest, an access mode, a runtime image or an accelerator variant in a domain
service, and do not branch on a specific vendor (`if profile == 'intel-xpu'`).
Adding a hardware is adding a profile/provider, not editing
cluster/deploy/evaluate/simulation/monitoring/profiling code. Follow
[the hardware plugin architecture](../../../docs/design/hardware-plugin-architecture.md)
and [the AGENTS hardware rule](../../../AGENTS.md#hardware-is-profile-driven-never-hardcoded).
Cover the profile contract and its consumers, not only one vendor.

## Shared file placement and naming

| Type | Directory | Filename | Export |
| --- | --- | --- | --- |
| Node shared transport or utility | `server/` | Responsibility-based `camelCase.ts` | camelCase function; PascalCase type/class |
| Node domain capability | `server/` or existing domain subdirectory | `<domain><Responsibility>.ts` | camelCase function; PascalCase type/class |
| MCP adapter | `server/mcp/` | `camelCase.ts` | camelCase function |
| Python shared base types | `llm_d_bench/core/` | `snake_case.py` | PascalCase class |
| Python HTTP adaptation | `llm_d_bench/api/` | `snake_case.py` | snake_case function |
| Python technical utility | `llm_d_bench/utils/` | Responsibility-based `snake_case.py` | snake_case function; PascalCase class |
| Python domain capability | `llm_d_bench/<domain>/` | Responsibility-based `snake_case.py` | snake_case function; PascalCase class |
| Python domain models | Owning domain directory | `models.py` or existing model module | PascalCase class |
| Node test | Beside the tested module | `<filename>.test.ts` or existing `.test.js` convention | Existing test conventions |
| Python test | Owning domain or utility directory | `test_<capability>.py` | `test_<behavior>` function |

Examples: `server/http.ts` / `fetchJsonWithTimeout`,
`server/clusterSources.ts` / `loadClusterSources`,
`llm_d_bench/core/exceptions.py` / `DomainError`,
`llm_d_bench/utils/kubernetes.py` / `validate_namespace`.

Extend existing owners and preserve public import compatibility; do not rename
unrelated legacy modules. Persistence placement follows the DB PR's data-access
design; do not recreate the reverted JSON Store abstraction. Unresolved shared
schema/task ownership is not a reason to place new domain models in those folders.
The human-facing reference is the
[shared code guide](../../../docs/refactoring/shared-code-guide.md).

## File storage

Use the [storage scheme](../../../docs/design/storage-layout.md) for exact domain paths.
Reuse `storage_path` / `prism_temp_root` in `llm_d_bench/utils/paths.py` and
`storagePath` in `server/storagePaths.ts`; do not introduce another storage root
variable or restore legacy directory overrides. `LENS_*` roots take precedence
over XDG defaults. Explicit source checkout inputs such as `LLM_D_ROOT` retain
their existing input semantics.

- Keep persistent task files under `data/artifacts/<domain>/<owner-id>`, records
  under `data/metadata`, datasets under `data/datasets`, and credentials under
  `data/credentials`. Follow existing domain layouts for their exceptions.
- Use cache/log/scratch roots for their respective purposes. Model weights stay
  on the selected storage volume; local model-cache metadata is not the weights.
- Reuse `register_artifacts`, `artifact_uri` and `resolve_artifact` from
  `utils/artifact_store.py` for retained task files. Register source version,
  configuration IDs, checksum, truncation/completeness and retention class;
  mark unavailable provenance unknown. Exclude credentials from public manifests.
- Use owner IDs and relative paths for portable references, not host absolute
  paths as permanent identities. Preserve third-party output names and keep
  read/write/download/delete callers consistent with the same layout.
- Naming and manifest field details remain in the scheme. Adding a file type
  uses these existing primitives; it does not require a new storage subsystem.

## Preserve the boundaries

Keep request parsing and response translation at the API boundary; keep business
rules in the owning service. Compare status codes, error payloads, validation,
authentication context, cancellation and task lifecycle before changing a shared
contract. Inspect both Node and Python boundaries when requests cross services.
For persistence changes, inspect existing records, schema/migration requirements,
path ownership and affected readers before changing formats or locations.
Do not merge domain models solely because their fields look alike.

When an API change affects browser clients, also apply
[ui](../ui/SKILL.md). When it changes container, cluster,
startup or runtime configuration behavior, also apply
[deployment](../deployment/SKILL.md).

## Verify the affected contract

Use the domain's existing tests and `CONTRIBUTING.md` / `Makefile` commands
(`make test-js`, `make test-python`, `npm run type-check`, as applicable).
Cover affected callers and changed error paths. Report separately which checks
used mocks and which exercised configured external services or a real cluster.
