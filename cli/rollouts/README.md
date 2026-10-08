# Manifest rollouts

`lens-rollouts` operates on `deployments/<model>/<environment>` in a separate
`curvebender-tools` Git checkout. It implements prepare, validate, and test.
LiteLLM configuration, traffic splitting, promotion, retirement, and automatic
rollback are outside this initial scope. Failures leave resources and evidence
available for inspection.

## Source contract

Each environment contains a self-contained `kustomization.yaml` rendering one
DisaggregatedSet and one or more ordered Helm values files for the standalone
router. All modelserver worker templates, including any explicit leader templates,
must match the router's `router.modelServers.matchLabels`. Shared prerequisites
(CRDs/controllers, namespace, secrets, network attachments, storage, etc.) are
provisioned separately. External Kustomize bases and symlinks are rejected so a
snapshot cannot silently read different content from the checkout or network.

Add `rollout-config.yaml` to each environment:

```yaml
schema: 1
namespace: llmd-netcheck
router:
  chart:
    name: llm-d-router-standalone
    repo: oci://ghcr.io/llm-d/charts
    version: v0.11.0 # example: select a version compatible with your values/plugins
  values:
    - router/router-fc-tenants.values.yaml
```

The namespace can instead come from the DisaggregatedSet. If `router.values` is
omitted, exactly one `router/*.values.yaml` is required. Multiple layers are
merged in listed order (maps recursively, lists/scalars replaced). `--chart` and
`--chart-version` override both refs. Store different chart versions in the two
Git revisions when testing a chart upgrade. Chart versions must be exact semver
pins; the package does not silently select `latest`.

The supplied practice environment has the chart repository/name configured but
deliberately leaves its version for the operator to choose. Its manifests must
exist in **both Git refs** before preparation; dirty/untracked files are never
used. A new environment needs a committed baseline before its first rollout.

## Prepare

```sh
lens-rollouts prepare \
  --repo /path/to/curvebender-tools \
  --from main \
  --to pr-123 \
  --model glm-5.3 \
  --environment practice-rollout \
  --chart-version v0.11.0 \
  --output /tmp/glm53-rollout
```

`--from` defaults to the checkout's local `main`; update that ref before preparing
if necessary. `--to` accepts a commit SHA (7–40 hex characters) or `pr-N`.
PRs fetch `refs/pull/N/head` from `origin` (override with `--remote`), rather than a
synthetic merge commit. Git/SSH authentication is inherited. Preparation never
checks out a branch or changes authored manifests. An existing output directory
is never overwritten. Without `--output`, snapshots go under
`REPO/.rollouts/<model>/<environment>/<from-sha>-<to-sha>/`.

```text
rollout.yaml                 # refs, commits, full hashes and version identities
v0/                          # --from
  source/                    # original environment files from Git
  inputs/content.yaml        # effective modelserver, values, chart and scope
  inputs/model-server.yaml   # source kustomization's rendered DisaggregatedSet
  kustomization.yaml         # generated labels, patches and helmCharts valuesInline
v1/                          # --to, same layout
```

Each pair is hashed before adding generated metadata. The SHA-256 input includes
the rendered DisaggregatedSet, normalized merged router values, chart coordinate
and version, model/environment, namespace, and schema version. YAML comments,
mapping order, and presentation whitespace do not affect it. Embedded YAML in
`router.epp.pluginsCustomConfig` is normalized too. String contents elsewhere
(including shell scripts and their comments) remain significant; stripping `#`
with a text regex could corrupt those values. List order and scalar types matter.
Git SHAs and generated overlays are excluded. An unchanged PR rebased or merged
therefore keeps its identity; a merge that changes effective configuration does not.

The full hash is recorded. The Kubernetes label uses `h-` plus 40 hex characters,
within Kubernetes' 63-character limit. This label is injected into every DS worker
and explicit leader template and the router's `modelServers.matchLabels`.
A router-only change intentionally creates a new modelserver version too, keeping
the rollout pair isolated. Identical effective inputs are reported as a no-op.

The generated kustomization uses `helmCharts.valuesInline`, so the same computed
label reaches Helm before its templates render. Kustomize patches the DS afterward.
This follows Kustomize's [Helm generation model](https://github.com/kubernetes-sigs/kustomize/blob/master/examples/chart.md).
It avoids trying to patch an ordinary values file as a Kubernetes resource.

DS names, router release names, Envoy ConfigMap references, and InferenceObjective
names are versioned. Requests using objective headers must use the generated
objective names from the rendered manifest. This does not change production
LiteLLM routing. Other fixed-name resources are rejected if they collide.

## Validate

```sh
lens-rollouts validate /tmp/glm53-rollout
# Optional admission/schema checks against an explicit cluster:
lens-rollouts validate /tmp/glm53-rollout --server-dry-run --context practice-cluster
```

Validation verifies snapshot/generated-input integrity, runs real Kustomize with
`--enable-helm`, checks resource names and namespaces, and checks pool/workload
selectors and v0/v1 isolation in both directions. Helm may download the pinned
chart during this step. The cached chart bytes are locked on first validation;
changes afterward fail validation. Treat registry versions as immutable: the
content identity includes the chart coordinate/version, not a registry digest.

Outputs are `rendered/v0.yaml`, `rendered/v1.yaml`, `validation.yaml`, and per-slot
`chart-lock.yaml`. Local validation is a render/consistency check, not a substitute
for API admission or live functional tests. Generated directories are not intended
to be edited; change the Git inputs and prepare a new rollout instead.

Kustomize renders Helm templates, and **kubectl owns the resulting resources**.
This workflow does not create Helm release records. Do not install the same
generated resources separately with Helm. Supported router mode is the standalone
Envoy proxy with an InferencePool and its HTTP port on `<release>-epp` Service.

## Test

```sh
# Render, validate, and save a reviewable plan. No cluster access:
lens-rollouts test /tmp/glm53-rollout --job /path/to/functional-job.yaml

# Deploy only v1 alongside an already managed, isolated v0:
lens-rollouts test /tmp/glm53-rollout \
  --context practice-cluster --job /path/to/functional-job.yaml --apply

# For an empty practice environment, also deploy the generated v0 first:
lens-rollouts test /tmp/glm53-rollout \
  --context practice-cluster --job /path/to/functional-job.yaml --bootstrap-v0 --apply
```

Live execution requires an explicit context and test Job. It validates again,
checks existing pool selectors and resource ownership, performs server dry-runs,
applies the candidate, waits for all requested modelserver role pods and router
workloads, then creates a uniquely named Job. It records `test-plan.yaml` and
`test-result.yaml`, returns nonzero on failure/timeout, and does not delete v0.
`--timeout` defaults to 1800 seconds per version's readiness and for the Job.

An existing deployment without these versioned names/labels is not automatically
adopted. Isolate/migrate it first, or use `--bootstrap-v0` in an empty practice
environment with enough capacity for both complete P/D sets. A broad legacy
InferencePool could otherwise discover candidate pods even when the candidate's
own selector is correct. Live preflight explicitly rejects that situation.

The functional-test team's manifest is an opaque `batch/v1` Job with
`restartPolicy: Never` or `OnFailure`. Its command/tests stay unchanged. The CLI
sets a unique `generateName`, the rollout namespace, and these environment
variables on its containers and init containers:

| Variable | Value |
| --- | --- |
| `ROLLOUT_ROUTER_URL` | Candidate router's in-cluster HTTP URL |
| `ROLLOUT_VERSION` | Candidate version label |
| `ROLLOUT_MODEL` | Model directory name |
| `ROLLOUT_ENVIRONMENT` | Environment directory name |

The Job must use those inputs (or an adapter in its command) to target v1. Job
completion is the test result; failure details and the Job name remain in
`test-result.yaml`. Inspect Job logs with kubectl. No placeholder test is reported
as a passed functional test.

## Development

From `cli/`, run `python -m pytest` and `ruff check rollouts tests`. Integration
tests use temporary Git repositories, a local Helm fixture, and the real
Kustomize/Helm binaries; they never contact Kubernetes. Install Kustomize 5.8.1+
and Helm to run them rather than skip them. Python-only tests cover errors and
Job status handling. Components are separated in the content schema so a future
LiteLLM adapter can participate without making gateway inputs mandatory today.
