---
name: deployment
description: Use when changing local startup scripts, Docker images or Compose, Kubernetes manifests, CI deployment configuration, or cluster deployment lifecycle behavior.
---

# Deployment

Follow [workflow](../workflow/SKILL.md) for discovery,
registration and unresolved decisions. Paths below are repo-relative.

## Choose the relevant entry point

| Scope | Inspect before changing |
| --- | --- |
| Local service start/restart | `scripts/dev.sh`, `scripts/run-backend.sh`, `package.json` |
| Containers | The relevant Dockerfile under the root or `docker/` |
| Cluster provisioning/deployment | `llm_d_bench/cluster/`, `llm_d_bench/deploy/`, the manifest/template used by the caller |
| CI | The relevant workflow under `.github/workflows/` |

For backend deployment logic, also apply
[backend](../backend/SKILL.md). Reuse the existing
runtime composition, configuration and Kubernetes operations for the relevant
lifecycle; do not create a second deployment path without examining callers.

## Hardware-driven deployment

Runtime images, accelerator variants, device-plugin/DRA manifests, NFD/GFD/DCGM
and driver access modes come from the cluster's selected hardware profile
(`deployment.runtime_image`, `driver` modes/manifests, telemetry contributions)
via the hardware registry — never a hardcoded per-vendor image or manifest in a
deploy path. A new accelerator is a new profile/provider, not another
`if vendor …` branch in the deploy providers. Resolve through the same contract as
[backend](../backend/SKILL.md#hardware-profiles); when a value is unset, disable
the feature cleanly instead of substituting another vendor's default. See
[the AGENTS hardware rule](../../../AGENTS.md#hardware-is-profile-driven-never-hardcoded).

## Kubernetes transport and compatibility

Read the [SDK/CLI operating guide](../../../docs/kubernetes-read-backend.md)
before changing cluster operations or backend-mode configuration, and follow
[backend](../backend/SKILL.md#kubernetes-operations) for reuse and test isolation.
With neither backend variable set, supported reads and writes use the official
SDK. `PRISM_KUBERNETES_BACKEND=cli` explicitly rolls back; the full backend setting
overrides the legacy read-only setting. Preserve this precedence.

Reuse the existing deployment runners and keep their scope/policy validation
before SDK or CLI execution. SDK failure is not permission to retry a write via
CLI. Client-side apply, Helm/Kustomize, exec, port-forward and other documented
exceptions retain CLI; keep their required tools installed. Do not substitute
server-side apply for client-side apply without addressing field ownership and
compatibility. Consult the operating guide for the complete current boundary.

## Local development

The normal user entry is `./dev.sh restart` from `scripts/`, equivalent to
`scripts/dev.sh restart` from the repository root. Inspect `scripts/dev.sh status`
and `$LENS_LOG_DIR/dev/` (default `~/.local/state/lens/logs/dev/`)
for service state and failures.

Preserve the startup preflight: `start` / `restart` generate and check the reuse
map before service changes. Pending refactor decisions warn without blocking local
start/restart; invalid registrations still block before stopping services. Keep
pending records intact. Audit/startup checks return 2 for any pending; task checks
return 2 for related or unknown scope. Local startup
uses `--purpose startup --base HEAD`, covers uncommitted source differences and
does not replace task-baseline verification or approve dependent refactor edits. Agents still register capabilities while coding.

For Docker-specific work, inspect actual service commands, mounts, ports and
runtime versions before choosing a launch command. The current Compose app uses
`node:20-alpine`, while `scripts/dev.sh` requires Node 22 or newer; these are not
interchangeable validated environments. Check MCP generation requirements in
`scripts/generate-mcp-tools.mjs` and startup code when changing Express startup.
Do not assume a host-generated catalog or Python environment exists in a container.

## Storage paths in launchers and containers

Follow the [storage scheme](../../../docs/design/storage-layout.md). Shell launchers
reuse `scripts/storage-env.sh`; Python and Node services use their shared path
helpers. Keep service environments and volume mounts consistent for shared files.
Resolve remote cache roots on the remote host rather than inserting the local
user's home path. Preserve explicit external source and model-volume inputs.
Changing configured paths neither migrates files nor switches running processes;
report those operations separately when they are part of the task.

## Validate proportionately

Use `bash -n` for changed shell scripts and build changed Dockerfiles
(`docker buildx build`), then relevant tests or image builds for the actual change.
Inspect persisted volumes, secrets, environment variables and rollback impact
before changing lifecycle behavior. A syntax check or image build does not prove
successful cluster deployment. State explicitly whether services were restarted
or a real cluster was exercised; execute those actions only within the user task.
