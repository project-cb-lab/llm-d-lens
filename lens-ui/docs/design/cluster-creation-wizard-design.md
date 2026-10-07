# Proposal: Cluster Creation Wizard

- **Status:** Draft — pending review.
- **Change directory:** `cluster-creation-wizard-design.md`
- **Reference architecture:**
  - Frontend: `src/components/OptimizationClusterOverview.jsx` (existing single-step
    "Add an existing k8s cluster" modal), `src/components/DataConnections/SubmitValidationPage.jsx`
    (existing multi-step wizard UI pattern that can be reused).
  - Backend: `llm_d_bench/cluster/` (registry/service/router),
    `llm_d_bench/storage/` (Storage volumes), `llm_d_bench/model_cache/` (Model Cache
    download job orchestration), `llm_d_bench/monitoring/cluster_stack/repo.py`
    (llm-d repository cache), `llm_d_bench/evaluate/router.py`
    (llm-d-benchmark repository cache, `_managed_benchmark_root`),
    `llm_d_bench/db/` (new; see
    `sqlalchemy-data-access-layer-design.md`: a unified data access
    layer that migrates structured data from JSON files to a relational database,
    with both external and embedded PostgreSQL connection modes).

---

## 1. Background and goals

Currently, "Create cluster" is a single-step modal in
`OptimizationClusterOverview.jsx`: the user fills in a name and description,
uploads a kubeconfig file, and after submission `POST /api/cluster/clusters`
performs synchronous validation and persistence
(`llm_d_bench/cluster/registry.py::create_cluster`). After the cluster is created,
the user still needs to manually do the following, separately:

1. Go to the Storage page and create a Storage volume (used as the persistence
   location for Model Cache).
2. Go to the Model Cache page to choose models, configure the HF Token source
   (currently only `HOST`—reading `~/.cache/huggingface/token` on the machine
   running the backend process—or `EXISTING_SECRET`—referencing a Secret that
   already exists in the cluster; there is **no** mode like "paste token directly
   and have Lens create a Secret inside the cluster"), and start the download.
3. Go to the Deploy/Evaluate pages to separately handle the llm-d and
   llm-d-benchmark version/commit:
   - On the Evaluate side, versioned repository caching already exists
     (`_BENCHMARK_REPOSITORY` / `_BENCHMARK_REVISION` environment variables +
     `_managed_benchmark_root()`, cached under
     `~/.cache/llm-d-bench/backends/<hash of repo+revision>`, with
     clone→checkout→create venv→pip install, and file locks to prevent duplicate
     concurrent installs).
   - The llm-d repository cache on the Monitoring side
     (`monitoring/cluster_stack/repo.py`) **has no version parameter**: it always
     clones the `main` branch into the fixed directory `~/.llm-d-bench/llm-d`,
     and cannot distinguish by commit/tag, so different clusters needing different
     versions would overwrite each other.

This proposal upgrades "Create cluster" from a single-step form into a
**multi-step wizard**, allowing operations / benchmark engineers who receive a new
Kubernetes cluster to complete in one pass: kubeconfig onboarding → network proxy
→ software version pinning → (optional) Model Cache prewarming, reducing the need
to manually chain together several pages afterward.

In addition, `sqlalchemy-data-access-layer-design.md` migrates
structured data such as the Cluster registry from JSON files to a relational
database, which creates a chicken-and-egg problem: the Lens backend process must
first know **how to connect to the database** before it can write the first
Cluster record. Currently the application has **no UI at all** for configuring a
database connection. This proposal adds that one-time, application-level (rather
than cluster-specific) configuration as a **prerequisite gate page (Step 0)** in
the wizard: when the application has not yet completed database configuration, the
"Create cluster" wizard first shows Step 0, and only proceeds to Step 1 after
successful completion (see §4.0).

### Goals

- The create-cluster wizard includes the following steps, in order:
  0. **Database setup** (shown only when the application has not yet been
     configured with a database, and only needs to succeed once globally):
     connect to an already deployed database, or choose to use embedded
     PostgreSQL.
  1. **Basic information** (same as today): name, description, kubeconfig file.
  2. **Network proxy**: `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`, with options to
     "use the default proxy" or "customize for this cluster".
  3. **Software versions**: llm-d and llm-d-benchmark version number or commit
     SHA; the backend prewarms the corresponding repository / install caches at
     creation time. Also pin the model-service data-plane version set (Gateway
     provider, GIE, Router, IPP) and install the shared components into the target
     cluster.
  4. **Model Cache (optional)**: the user provides `HF_TOKEN`, Lens creates a
     Secret in the target cluster; choose or create a Storage volume; choose which
     models to prewarm (HuggingFace repo id + revision); automatically trigger the
     download after creation.
  5. **Confirmation**: read-only summary, then submit.
- Afterward, modules such as Evaluate and Monitoring should, for the same cluster,
  use the llm-d / llm-d-benchmark versions pinned on that cluster **by default**,
  without requiring the user to override them via environment variables or request
  bodies each time.
- Steps 2-4 are all optional / skippable: skipping is equivalent to the current
  behavior (no proxy override, use global default versions, no model prewarming),
  ensuring the existing minimal flow of "only pass kubeconfig" and existing
  automation scripts are not broken (the existing required fields of
  `POST /api/cluster/clusters` remain unchanged; all added fields are optional).

### Non-goals

- Do not implement "automatic discovery of the cluster's own proxy configuration"
  (for example probing a proxy ConfigMap in kube-system or node
  `/etc/environment`). The precise meaning of "use the cluster's own proxy" in
  this proposal is described in §4.2 (intentionally simplified and marked as an
  assumption pending confirmation).
- Do not introduce a new Model Catalog data source; the Model Cache step supports
  only HuggingFace (same scope as the current `model_cache` module;
  `ModelSourceKind.MODEL_CATALOG` remains a placeholder).
- Do not redesign the independent operation capabilities of the existing Storage /
  Model Cache pages—the wizard only orchestrates their "creation" into the
  create-cluster flow. Those two pages remain independently usable, and
  wizard-created Storage volumes and Model Cache entries are indistinguishable from
  manually created ones, sharing the same store/API.
- Step 0 (database setup) is **not one configuration per cluster**, and is not
  attached to any `Cluster` record: it is a global setting at the Lens backend
  process level. Once configured, it applies to all clusters and all future
  create-cluster wizards, and remains hidden thereafter (see §4.0). This proposal
  does not implement later operational capabilities like "migrate an existing
  database to another database", which belongs to
  `sqlalchemy-data-access-layer-design.md`.
- Do not expose advanced database parameters in the wizard (pool size, read
  replicas, SSL certificate upload, etc.); Step 0 covers only the two minimal
  cases of "use embedded" or "provide a connection string". Advanced parameters
  continue to use environment variables (see
  `sqlalchemy-data-access-layer-design.md` §4.1 such as
  `LLM_D_BENCH_DB_POOL_SIZE`).

---

## 2. Overall design

### 2.1 Frontend: multi-step wizard

In `OptimizationClusterOverview.jsx`, replace the current single-form
`Modal(showCreate)` with a wizard modal with step indicators (visually based on
the `wizardStep` / step-dot implementation in `SubmitValidationPage.jsx`),
`size="lg"`:

```
When the application has not yet configured a database, opening the wizard first
shows a standalone prerequisite page (not counted in the step indicator):

┌───────────────────────────────────────────────────────────┐
│                    ⓪ Database setup (required the first time) │
├───────────────────────────────────────────────────────────┤
│   ○ Connect to an existing database (fill in connection info) │
│   ○ Use embedded PostgreSQL (no additional input required)    │
├───────────────────────────────────────────────────────────┤
│                                        [Test connection and continue] │
└───────────────────────────────────────────────────────────┘

After success (or when the application has already been configured, so Step 0 is
skipped directly), the normal five-step wizard is shown:

┌───────────────────────────────────────────────────────────┐
│  ① Basic info   ② Network proxy   ③ Software versions   ④ Model Cache   ⑤ Confirm  │
├───────────────────────────────────────────────────────────┤
│                     <current step form content>                       │
├───────────────────────────────────────────────────────────┤
│                              [Previous]  [Next / Create cluster]     │
└───────────────────────────────────────────────────────────┘
```

- Step 0 does not appear in the ①-⑤ step indicator and has no "skip" switch—it
  is a one-time **prerequisite gate**, not a configuration item of the cluster
  itself. When the application has already completed database setup, the wizard
  begins rendering directly from Step 1 and the user never sees Step 0 at all
  (see §4.0).
- The local form state of each step is merged into a single `wizardForm` object
  (instead of the current three fields `{ name, description, file }`, expanded to
  the full payload in §3).
- Steps 2-4 each have a "skip this step" switch at the top, off by default. When
  off, that step's form fields are not rendered, not validated, and the submitted
  payload does not include the corresponding segment.
- After clicking "Create cluster", the wizard **does not close immediately**:
  instead, it enters a "creating" progress view (see the state machine in §5) and
  polls the cluster's provisioning status until all selected steps are completed
  or failed, and only then allows closing or retrying individual failed substeps.
  This differs from the current behavior (synchronous creation and immediate modal
  close), as detailed in §5.

### 2.2 Backend: from "one-shot synchronous creation" to "creation + background orchestration"

Currently `POST /api/cluster/clusters` is synchronous: write metadata + kubeconfig
file, then immediately return 201. That step (§3 Step 1) remains synchronously
unchanged—it is inherently fast and is a prerequisite dependency for later steps
(without `cluster_id`, proxy configuration, Storage volume, and Model Cache
cannot proceed).

Steps 2-4 **cannot** be stuffed into the same synchronous request:

- Step 3 (software version prewarming) may trigger git clone + `pip install`
  (`_install_benchmark` already needs a 30-minute timeout today), so it must be
  asynchronous.
- Step 4 (Model Cache) is already asynchronous
  (`ModelCacheService.provision()` is a "fire-and-forget background task"), and
  depends on the Storage volume created in Step 4 becoming `READY` first
  (`storage/service.py::create_volume_objects` also does asynchronous
  `kubectl apply` + verification).

Therefore the create-cluster backend flow changes as follows:

1. `POST /api/cluster/clusters` (existing required fields remain: `name`,
   `description`, `file`; new optional fields are listed in §3) completes
   synchronously:
   - Write Cluster metadata + kubeconfig (unchanged from today).
   - **Synchronously** write fields submitted from Steps 2/3 (proxy config,
     llm-d/llm-d-benchmark versions) into the expanded Cluster metadata (these are
     only persisted configuration and do not involve network I/O).
   - If Step 4 enabled Model Cache, **synchronously** validate input
     (repo id format, non-empty token, etc.), but do not block waiting for the
     Storage volume to become ready or for the download to finish.
   - Return `202` (instead of today's `201`—semantically more accurate to express
     "background work still remains"), with a new `provisioning` section in the
     response body (see §5), which the frontend uses to decide whether to show the
     progress view.
   - When no optional steps are selected (equivalent to current behavior),
     `provisioning` is empty / `null`, and the frontend behaves exactly as it does
     today (close immediately after creation).
2. A new background task
   `llm_d_bench/cluster/provisioning.py::provision_cluster(cluster_id)`
   (`asyncio.create_task`, using the same fire-and-forget task pattern as the
   existing `deployment_run_manager` and `model_cache`) executes the selected
   substeps in order:
   a. If Model Cache is enabled: call `storage.service` to create a `local-disk`
      Storage volume (see §4.4 for the discussion of automatic configuration), and
      wait for it to become `READY`.
   b. If Model Cache is enabled: call the new
      `model_cache.jobs.create_pasted_token_secret()` to create a Secret in the
      target cluster using the user-provided `HF_TOKEN` (see §4.4), and then call
      existing `ModelCacheService.request_download()` + `provision()`.
   c. If software version pinning is enabled: call the new utility (see §4.3)
      `llm_d_bench/utils/repo_cache.py::ensure_cached(name, url, ref)` to prewarm
      caches for llm-d and llm-d-benchmark respectively.
   - Each substep updates the Cluster's provisioning status record (see §5), which
     the frontend polls.
3. The frontend polls the new endpoint
   `GET /api/cluster/clusters/{id}/provisioning` until all substeps are
   `ready`/`failed`.

---

## 3. Data model changes

### 3.0 (new) `llm_d_bench/db/bootstrap_config.py`: application-level database configuration

This configuration describes **how the Lens backend process connects to the
database**, so it **cannot be stored in the database itself** (chicken-and-egg).
It is persisted in a small standalone file that exists before the database,
written atomically the same way as other stores (temporary file + `os.replace`),
at `~/.llm-d-bench/db-bootstrap.json` (overridable via
`LLM_D_BENCH_DB_BOOTSTRAP_FILE` to support containerized deployments mounting it
to a persistent volume):

```python
class DatabaseBootstrapConfig(StrictModel):
    mode: Literal["external", "embedded"]
    # Required when mode="external"; no redaction before or after writing to disk
    # (this file itself is the only source of "how to connect to the database"),
    # but it must never be echoed back verbatim through any read-only API; see §4.0.
    database_url: str | None = Field(default=None, alias="databaseUrl")
    configured_at: datetime = Field(default_factory=utcnow, alias="configuredAt")


class DatabaseStatus(StrictModel):
    """Response of `GET /api/v1/system/database`; only exposes whether it is
    configured and which mode, and never echoes `database_url`
    (which may contain a password)."""
    configured: bool
    mode: Literal["external", "embedded"] | None = None
    # In external mode, redacted display info (host/port/dbname), for read-only
    # hints such as "Connected to xxx:5432/llm_d_bench"; password/username do not
    # appear here.
    display_target: str | None = Field(default=None, alias="displayTarget")


class DatabaseSetupRequest(StrictModel):
    mode: Literal["external", "embedded"]
    database_url: str | None = Field(default=None, alias="databaseUrl")

    @model_validator(mode="after")
    def _require_url_for_external(self):
        if self.mode == "external" and not (self.database_url or "").strip():
            raise ValueError("external mode requires databaseUrl")
        if self.mode == "embedded" and self.database_url:
            raise ValueError("embedded mode must not include databaseUrl")
        return self
```

This section is independent from the rest of §3.1-3.2: `DatabaseBootstrapConfig`
is not attached to `Cluster` / `ClusterRecord`, and does not appear in the
`POST /api/cluster/clusters` payload (see Non-goal #4 in §1).

### 3.1 `llm_d_bench/cluster/registry.py::Cluster`

Currently `Cluster` is a frozen dataclass with only four fields. Expand it to:

```python
@dataclass(frozen=True)
class Cluster:
    id: str
    name: str
    description: str
    created_at: str
    proxy: ProxyConfig | None = None  # new; see below
    llm_d_ref: str | None = None  # new: llm-d version or commit SHA
    llm_d_benchmark_ref: str | None = None  # new: llm-d-benchmark version or commit SHA
    data_plane: DataPlaneVersions | None = None  # new: model-service data-plane version set
```

```python
@dataclass(frozen=True)
class DataPlaneVersions:
    """Pinned version set for the model-service data plane; components are
    compatibility-constrained, so they are selected as a group."""

    gateway_provider: str | None = None    # istio | gke | agentgateway | envoy-ai-gateway
    gateway_version: str | None = None
    gie_version: str | None = None         # GAIE / Gateway API CRD release
    router_version: str | None = None      # llm-d Router (EPP / Proxy) chart or image
    ipp_version: str | None = None         # llm-d-inference-payload-processor chart or image
```

```python
@dataclass(frozen=True)
class ProxyConfig:
    mode: Literal["auto", "custom"] = "auto"
    # When mode="custom", the following three are the effective values (user-entered).
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None
    # When mode="auto", the following are a detected snapshot (read-only, may be
    # re-detected and overwritten at any time); on detection failure all three are
    # None and detection_error is non-empty.
    detected_http_proxy: str | None = None
    detected_https_proxy: str | None = None
    detected_no_proxy: str | None = None
    detected_at: str | None = None  # ISO timestamp of the most recent successful/failed detection
    detection_error: str | None = None  # human-readable reason when detection fails
```

- `mode="auto"`: the detection flow in §4.2 writes `detected_*` /
  `detected_at` / `detection_error`; the runtime effective values use
  `detected_http_proxy` and friends (if detection fails or they are empty, this is
  treated as "no proxy", and no longer falls back to the Lens backend process's
  own environment variables—that was the old design. "auto" is now explicitly
  "detect this target cluster", not "inherit the backend process").
- `mode="custom"`: ignore `detected_*`; runtime effective values use the
  manually entered `http_proxy` etc.
- Add corresponding keys to the metadata JSON and expand
  `Cluster.from_dict` / `to_dict` accordingly. When old files lack these keys,
  treat them all as `None` / `mode="auto"` and "not yet detected", which is
  backward-compatible without a migration script. When first reading an old
  cluster, the `ready` / overview APIs could opportunistically trigger one
  backfill detection, or we could simply require the user to click "re-detect
  proxy" once in the cluster detail page, avoiding silently stuffing a potentially
  slow I/O operation into an endpoint that was originally just a query—the latter
  is preferable, and better matches the current code style where "detection-like
  operations are explicit endpoints", such as `cluster_is_ready()` being
  explicitly awaited inside `list_clusters` / `overview`, but that is only a
  single `--raw=/readyz` probe on a different scale.

### 3.2 `llm_d_bench/cluster/models.py`

```python
class ProxyConfigDTO(StrictModel):
    mode: Literal["auto", "custom"] = "auto"
    http_proxy: str | None = Field(default=None, alias="httpProxy")
    https_proxy: str | None = Field(default=None, alias="httpsProxy")
    no_proxy: str | None = Field(default=None, alias="noProxy")
    detected_http_proxy: str | None = Field(default=None, alias="detectedHttpProxy")
    detected_https_proxy: str | None = Field(default=None, alias="detectedHttpsProxy")
    detected_no_proxy: str | None = Field(default=None, alias="detectedNoProxy")
    detected_at: str | None = Field(default=None, alias="detectedAt")
    detection_error: str | None = Field(default=None, alias="detectionError")


class HfTokenSecretCreateRequest(StrictModel):
    """Standalone "create HF Token Secret" request, not attached to a specific
    Model Cache entry."""
    namespace: str = Field(min_length=1, max_length=253)
    # Default name includes a short UUID suffix to avoid name collisions; users
    # can also provide their own, in which case it must be unique within the
    # namespace.
    name: str = Field(default_factory=lambda: f"hf-token-{uuid4().hex[:8]}", max_length=253)
    token: str = Field(min_length=1)


class HfTokenSecretRef(StrictModel):
    namespace: str
    name: str


class ModelCacheSeedRequest(StrictModel):
    """Step 4 payload; submitted together with the create request only when the
    user did not skip this step.

    Storage volumes no longer have an "auto-create" default capacity/path—
    `storage` is either an existing volume id, or a fully completed
    `StorageVolumeCreateRequest` entered manually in the wizard
    (exactly the same fields as creating one manually on the Storage page; see 4.4).
    """

    hf_token_secret: HfTokenSecretCreateRequest = Field(alias="hfTokenSecret")
    repo_id: str = Field(alias="repoId", pattern=_REPO_ID_PATTERN)  # reuse model_cache regex
    revision: str = Field(default="main")
    storage: str | StorageVolumeCreateRequest  # existing volume id, or complete create-volume form


class ClusterRecord(StrictModel):
    id: str
    name: str
    description: str
    created_at: str = Field(alias="createdAt")
    ready: bool = False
    proxy: ProxyConfigDTO = Field(default_factory=ProxyConfigDTO)  # new
    llm_d_ref: str | None = Field(default=None, alias="llmDRef")  # new
    llm_d_benchmark_ref: str | None = Field(default=None, alias="llmDBenchmarkRef")  # new
    data_plane: DataPlaneVersionsDTO | None = Field(default=None, alias="dataPlane")  # new


class DataPlaneVersionsDTO(StrictModel):
    """Pinned version set for the model-service data plane; selected as a group, see 4.3."""
    gateway_provider: str | None = Field(default=None, alias="gatewayProvider")
    gateway_version: str | None = Field(default=None, alias="gatewayVersion")
    gie_version: str | None = Field(default=None, alias="gieVersion")
    router_version: str | None = Field(default=None, alias="routerVersion")
    ipp_version: str | None = Field(default=None, alias="ippVersion")


class ClusterProvisioningStep(StrictModel):
    """Status of a provisioning substep, used by the frontend to render the progress view."""

    id: Literal["proxy-detection", "storage", "model-cache", "llm-d-repo", "llm-d-benchmark-repo", "data-plane"]
    status: Literal["skipped", "pending", "running", "ready", "failed"] = "skipped"
    detail: str | None = None  # human-readable current action, e.g. "cloning llm-d@<sha>"
    failure_detail: str | None = Field(default=None, alias="failureDetail")


class ClusterProvisioningStatus(StrictModel):
    cluster_id: str = Field(alias="clusterId")
    steps: list[ClusterProvisioningStep]

    @property
    def done(self) -> bool:
        return all(step.status in ("skipped", "ready", "failed") for step in self.steps)
```

`POST /api/cluster/clusters` adds the following form fields (still
`multipart/form-data`, because kubeconfig is a file upload):

| Field | Type | Description |
|---|---|---|
| `proxy` | JSON string (`{"mode": "auto"}` or `{"mode": "custom", "httpProxy": ..., ...}`) | Optional; default `{"mode": "auto"}`; when `mode="auto"`, the background task triggers one detection after creation (§4.2) |
| `llmDRef` | string | Optional |
| `llmDBenchmarkRef` | string | Optional |
| `dataPlane` | JSON string (`DataPlaneVersionsDTO`) | Optional; pinned version set for the model-service data plane (provider / GIE / Router / IPP), see §4.3 |
| `modelCacheSeed` | JSON string (`ModelCacheSeedRequest`) | Optional; present means Step 4 was not skipped |

The response body expands to:

```json
{
  "cluster": { "...ClusterRecord..." },
  "sessionId": "…",
  "provisioning": { "clusterId": "…", "steps": [ /* ClusterProvisioningStep[] */ ] }
}
```

When `provisioning` is `null`, it means there are no background substeps
(equivalent to today's behavior).

New endpoints for Step 0 (fully independent from `POST /api/cluster/clusters`, with
no `clusterId`; see §4.0):

| Endpoint | Description |
|---|---|
| `GET /api/v1/system/database` | Returns `DatabaseStatus`; the wizard calls this first when it opens to decide whether to render Step 0 |
| `POST /api/v1/system/database` | Request body `DatabaseSetupRequest`; on success, persist `DatabaseBootstrapConfig`, rebuild the database engine, and run one Alembic migration, then return `DatabaseStatus`; on failure (for example external mode cannot connect), return 409/422 + human-readable reason, and Step 0 remains visible |

---

## 4. Detailed per-step design

### 4.0 Step 0: database setup (first-time global gate, unrelated to any specific cluster)

> **Update (subsequent change):** This section originally designed Step 0 as a
> prerequisite page inside the "Create cluster" wizard. It has now been changed to
> be completed once in the installer script
> `scripts/LensInstaller-Ubuntu-x86_64.sh` (new
> `configure_database` / `db_interactive_configure` steps; silent install
> `--silent` defaults directly to embedded PostgreSQL, while interactive install
> prompts for the same fields described here), and no longer appears in the
> wizard. Reason: database connection is an operational decision about **how the
> entire backend process connects to its own database**, and asking once during
> installation better matches the user's mental model than checking "is it
> configured?" every time the wizard opens. The `POST /api/v1/system/database`
> API itself (together with the probing/migration/persistence behavior described
> below) **remains unchanged**; only the caller changes from the React frontend to
> the new `llm_d_bench/db/bootstrap_cli.py` (both share the same
> `apply_database_setup()` implementation). `GET /api/v1/system/database` also
> remains, for operational status queries. `CreateClusterWizard.jsx` now only
> performs a read-only "is it configured?" check when opened, and if not
> configured, prompts the user to rerun the installer script instead of rendering
> the form described below. The body below is retained as a design record of that
> earlier form; the fields/validation logic were moved unchanged into the
> installer script.

**Trigger condition:** when the wizard modal opens, first call
`GET /api/v1/system/database`; only when `configured: false` should Step 0 be
rendered. If `true`, jump directly to Step 1 (the user notices nothing). This
check runs every time the wizard opens (rather than only once and caching it in
frontend local state), because `DATABASE_URL` / `LLM_D_BENCH_DB_MODE` may also
already be configured through backend startup environment variables (see
`sqlalchemy-data-access-layer-design.md` §4.1); that should likewise count as
"already configured" and skip Step 0.

**Form:** two mutually exclusive radio options; the corresponding input area
expands only after selection:
1. **Connect to an existing deployed database**: first choose the database engine
   (PostgreSQL / MySQL / Oracle / SQL Server dropdown, corresponding to
   `settings.py::SUPPORTED_EXTERNAL_ENGINES`), then fill in five separate fields:
   Host, Port (default filled based on engine, e.g. PostgreSQL 5432 / MySQL 3306 /
   Oracle 1521 / SQL Server 1433, editable), Database name, Username, Password—
   the user is **not** required to directly enter / assemble a full connection
   string. The password field is masked with `type="password"`. Before
   submission, the frontend assembles these five fields into the single DSN format
   already defined by the backend (`******host:port/dbname`, with user/password
   URL-encoded before assembly), then submits that together with the engine to
   `POST /api/v1/system/database`—the backend API itself remains unchanged and
   still only accepts / validates a complete `databaseUrl` (consistent with the
   `DATABASE_URL` format in `sqlalchemy-data-access-layer-design.md` §4.1); the
   assembly logic happens only in the frontend. The selected engine must match the
   DSN scheme (`engine://user:password@host:port/dbname`) in the assembled URL (validated by backend `DatabaseSetupRequest`,
   422 on mismatch). **Only PostgreSQL is end-to-end verified** (migration + DAO
   layer fully covered); MySQL / Oracle / SQL Server currently have only
   "UI + driver"-level experimental support—they may establish a connection, but
   some migrations use PostgreSQL-specific types (such as `sa.ARRAY`), so
   `alembic upgrade head` may fail on those engines. When a non-PostgreSQL engine
   is selected, the wizard shows a prominent experimental-support warning.
2. **Use embedded PostgreSQL**: no input is required; selecting it is sufficient
   to click submit (corresponding to the embedded mode in
   `sqlalchemy-data-access-layer-design.md` §4.3, where `pgserver` automatically
   runs `initdb` under `~/.llm-d-bench/embedded-pg/data`). Embedded mode is
   **always PostgreSQL** (`pgserver` only provides that engine), and does not
   offer engine selection.
   `sqlalchemy-data-access-layer-design.md` §4.3 embedded mode, where `pgserver`
   automatically `initdb` to `~/.llm-d-bench/embedded-pg/data`). Embedded mode is
   **always PostgreSQL** (`pgserver` only provides that one engine) and offers no
   engine selector.

**Submission behavior** (`POST /api/v1/system/database`, see the API table in
§3.2):

- External mode: the backend first performs a connectivity probe (`SELECT 1`).
  After success, persist `DatabaseBootstrapConfig` and run
  `alembic upgrade head`; if the probe fails (network unreachable / authentication
  failure / invalid DSN / missing driver package for the engine), return 422/409 +
  the specific reason (when the driver is missing, include a hint such as
  `pip install llm-d-prism-backend[mysql|oracle|mssql]`). Step 0 remains visible
  and does not allow proceeding; the user can correct the connection string and
  retry, or switch to embedded mode.
- Embedded mode: call `pgserver.get_server(data_dir)` (initializing with `initdb`
  on first use), obtain the DSN, then likewise run one migration. This typically
  completes within a few seconds, so the frontend only needs a brief loading
  state and does not need the asynchronous Provisioning state machine in §5
  (the database must be synchronously ready before the wizard can continue; there
  is no meaningful intermediate state such as "allow failure and retry later").
- After success in either mode, the application-level database is considered
  configured. Thereafter `GET /api/v1/system/database` returns
  `configured: true`, and Step 0 is **permanently skipped** (unless operations
  manually delete `db-bootstrap.json` or change environment variables—which is an
  operational action outside the scope of this wizard).

**Relationship to the cluster itself:** only after Step 0 succeeds does Step 1
`registry.create_cluster()` actually write to the database for the first time
(rather than today's local JSON files). But this change is completely transparent
to the rest of Steps 1-5: data models such as `Cluster` / `ClusterRecord`
(§3.1-3.2) do not need to know whether the underlying storage is files or a
database; the DAO layer absorbs that difference (see
`sqlalchemy-data-access-layer-design.md` §6.3).

**Concurrency / multi-replica considerations:** if multiple backend replicas start
for the first time simultaneously and receive Step 0 submissions at the same time,
persist `db-bootstrap.json` using the same "temporary file + `os.replace`"
atomic-write strategy as other stores; `alembic upgrade head` itself is idempotent
(running it twice does not create tables twice), so even if two replicas both
submit once, data corruption will not occur. At worst, one overwrites the other's
`configured_at` timestamp, which is acceptable because under normal operation both
submissions should use the same mode/connection string.

### 4.1 Step 1: basic information (unchanged from current behavior)

Name, description, kubeconfig file; after submission, immediately persist with the
existing `registry.create_cluster()` to obtain `cluster_id` for later steps.
**Inside the wizard**, the create API is actually called when the user clicks
"Next" to leave Step 1 (rather than waiting until the final "Confirm" step),
because Steps 2-4 all require an existing `cluster_id` (especially because
kubeconfig must be persisted first before Step 4's Storage / Model Cache can issue
kubectl commands to the target cluster). If the user cancels midway through Steps
2-4, the wizard must call `DELETE /api/cluster/clusters/{id}` to roll back
(reusing the current "delete cluster" logic; since there is not yet any Storage
volume at this point, the new "cannot delete a cluster with Storage underneath it"
check added in §8.4 will not block this rollback).

> This is an intentional design tradeoff: splitting "create" into "immediately
> create in Step 1 + Step 2-4 are follow-up PATCH operations", rather than
> "collect the full wizard form and submit once at the end". Reason: kubeconfig
> validation itself may fail (file format error, cannot connect to cluster), and
> the user should get that feedback in Step 1 rather than after filling out all
> four steps only to discover the very first and most fundamental one was invalid.

### 4.2 Step 2: network proxy

**Semantics clarification (confirmed with the requester):** selecting "use the
cluster's own proxy" means **automatically detecting the proxy configured on the
target Kubernetes cluster itself**, not "inherit the environment variables of the
Lens backend process" (that assumption in the old draft was wrong and has been
discarded).

Form fields:

```
( ) Automatically detect this cluster's proxy (recommended)
    [Detected] HTTP_PROXY=http://10.x.x.x:3128  HTTPS_PROXY=...  NO_PROXY=...
             (detected at 2024-xx-xx 12:00, [Detect again])
    or
    [Detection failed] Could not detect proxy configuration from the cluster: <reason>.
             Will run as "no proxy";
             if a proxy is actually required, switch to "Custom" and fill it in manually.
( ) Custom proxy for this cluster
    HTTP_PROXY  [__________]
    HTTPS_PROXY [__________]
    NO_PROXY    [__________]
```

**Detection mechanism:** Kubernetes itself has no first-class concept of a
"cluster-level proxy" (proxy is usually runtime configuration of each node's
containerd/docker/kubelet, or system-level environment variables in
`/etc/environment`), so detection can only "guess using a method that can read
node/system configuration". Proposal:

1. Submit a one-off **diagnostic Job** to the target cluster (`kind: Job`,
   `restartPolicy: Never`, `activeDeadlineSeconds` with a short timeout such as
   60s; reusing the existing "temporary Job + poll + cleanup" pattern in
   `deploy/runtime`). The Job's single container does two things and writes the
   result to container stdout (the backend reads the result from `kubectl logs`,
   so no extra API is required):
   - Directly read the `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` environment
     variables inherited by the container, plus lowercase variants (in many
     clusters kubelet propagates node-level proxy settings into containers, which
     is the most common and highest-success-rate path).
   - If those are empty, attempt a read-only hostPath mount of `/etc/environment`
     (Debian/Ubuntu) and `/etc/profile.d/proxy.sh` (if present) and use regex
     extraction as a fallback.
2. **Known limitations (documented explicitly, not hidden):**
   - If the cluster enforces stricter PodSecurity Admission (`restricted`
     profile), hostPath mounts may be rejected outright. In that case the
     diagnostic Job only has the "container inherited environment variables" path;
     if that produces nothing, then it simply produces nothing. That should be
     treated as "proxy not detected" (not an error, but one reasonable result
     indicating the cluster may not have a proxy).
   - The detection result is only a point-in-time snapshot and is not
     continuously synchronized; if the cluster-side proxy configuration changes,
     the user must manually click "Detect again" to refresh it (no periodic
     polling; the cost/benefit is not worth it).
   - When detection fails or finds nothing, **do not fall back to any other
     source** (do not read the Lens backend's own environment variables). In the
     semantics of "automatic detection", only the detection result is trusted; if
     nothing is found, it means "this cluster has no proxy", avoiding confusing
     results like "detection found A, but fallback mixed in backend process B".
   - If the user believes detection is wrong or the environment is unusual, they
     can switch directly to "Custom" and fill values manually. This is the final
     fallback that guarantees the feature remains usable even if detection success
     rate is low.
3. Phase split is described in §8: detection via diagnostic Job is new
   infrastructure with higher risk than the other steps, so it is recommended to
   split it into its own Phase. In Phase 1, only the manually entered custom proxy
   option can be shipped first (that is, the "automatic detection" button starts
   disabled/hidden), and the detection capability is added in Phase 2.

**How it takes effect at runtime:** after saving `ProxyConfig` via
`PATCH /api/cluster/clusters/{id}` (when `mode="auto"`, the background
Provisioning task triggers one detection and writes back `detected_*`; when
`mode="custom"`, the manual values take effect directly),
`llm_d_bench/utils/kubernetes.py::kubeconfig_environment()` and
`model_cache/jobs.py::_proxy_environment()` are changed to preferentially read the
"effective values" of that cluster's `ProxyConfig` (`custom` uses the manually
entered values; `auto` uses the `detected_*` values; when all are empty, no
proxy-related environment variables are set), no longer reading `os.environ`, so
that:

- Model Cache download Jobs (`jobs.py`)
- Pods launched by the Deploy side
  (`deploy/runtime/composition.py::_model_environment`, which currently also reads
  proxy values from `os.environ`, and is likewise migrated to "read the effective
  value from this cluster's `ProxyConfig`")
- Any future in-cluster workload that needs to access the public internet

all uniformly follow the rule "cluster-level proxy configuration; if absent, fall
back to the process-level default", instead of, as today, each module scattering
its own direct reads of `os.environ`.

### 4.3 Step 3: software versions (llm-d / llm-d-benchmark / data plane)

Form fields:

```
llm-d version/commit         [main ▾]  (dropdown of common tags + free-form commit SHA)
llm-d-benchmark version/commit [87d03da…] (default = current _BENCHMARK_REVISION)

── Model-service data plane (pinned version set per cluster; optional) ──────────
Gateway provider             [istio ▾]  (istio | gke | agentgateway | envoy-ai-gateway)
Gateway / provider version   [v1.29.2]
GIE / Gateway API CRD        [v1.5.0]
llm-d Router version         [v0.10.0]  (EPP / Proxy chart or image)
IPP version                  [v0.1.0-rc.4]  (payload-processor chart or image)
```

**Backend changes (reuse + generalize the two existing repository caches rather
than inventing a new one):**

1. Add `llm_d_bench/utils/repo_cache.py`, extracting the reusable parts of
   `_managed_benchmark_root` / `_install_benchmark` from `evaluate/router.py`
   (clone → checkout → optional build step → file lock against concurrent
   duplicate installs):

   ```python
   async def ensure_cached(
       *,
       name: str,
       repository: str,
       ref: str,
       build: Callable[[Path], None] | None = None,
   ) -> Path:
       """Return <cache_root>/<name>/<key(repository, ref)>. If missing, do
       clone+checkout (+ optional build); use file locks to prevent duplicate
       concurrent installation. `name` separates cache buckets for llm-d,
       llm-d-benchmark, etc."""
   ```

   `cache_root` defaults to `~/.cache/llm-d-bench/repos` (consistent with the
   existing `~/.cache/llm-d-bench/*` directory family). The existing Evaluate path
   `~/.cache/llm-d-bench/backends/<hash>` can be converged into
   `repos/llm-d-benchmark/<ref>` in the next major version. In the first version
   of this proposal, keep Evaluate unchanged and only let new code use the new
   utility function, to avoid an overly broad change to stable existing paths.
2. Change `monitoring/cluster_stack/repo.py` to accept a `ref` parameter (it
   currently hardcodes `--branch main`); the default remains `main` (preserving
   current behavior), and when `ref` is provided use
   `ensure_cached(name="llm-d", repository=_LLM_D_REPO_URL, ref=ref)`.
3. Keep `evaluate/router.py::_managed_benchmark_root` unchanged in signature and
   behavior (avoiding impact to currently running evaluate tasks and their
   existing `~/.cache/llm-d-bench/backends` path), but add a resolution priority:
   if the request does not explicitly pass repository/revision, and the current
   cluster identified by `cluster_session_id` has `llm_d_benchmark_ref`, use that
   as the default. Today's priority is "request body > environment variable
   default"; change it to "request body > cluster default > environment variable
   default".
4. When Step 3 is not skipped, the Cluster creation wizard's background task calls
   `ensure_cached(...)` once for llm-d and once for llm-d-benchmark (prewarm only,
   not immediate use), moving the "invisible backend wait" earlier to cluster
   creation rather than making the user wait for a multi-minute git clone + pip
   install the first time they click "run evaluate".

**Data-plane components (different from the two repositories above: installed
into the target cluster, not cached as local source):**

The model-service data plane (Gateway/Proxy + IPP + InferencePool + EPP) needs a
pinned version per cluster, but these are **Helm charts / images installed into the
target cluster**, not source trees Lens uses locally, so the `ensure_cached`
clone-source approach does not apply:

- **Version set**: `data_plane` (`DataPlaneVersionsDTO`) records the provider, GIE,
  Router, and IPP versions. These components are compatibility-constrained
  (IPP ↔ Router ↔ GIE ↔ provider ↔ Kubernetes); validate them **as a group** and
  reject any mismatch instead of allowing independent free choice.
- **Shared components installed at cluster creation**: Gateway API / GAIE CRDs,
  Gateway provider, Gateway, and IPP are installed during cluster creation (new
  provisioning substep `data-plane`), so publishing a model later only adds
  InferencePool / HTTPRoute / IPP config.
- **Model-level components installed at publish time**: InferencePool, EPP,
  HTTPRoute, and the IPP model-mapping ConfigMap are rendered and installed by the
  model-service Reconciler at the cluster's pinned versions.
- **Compatibility check**: at cluster creation, run a preflight over provider vs.
  Kubernetes version, GIE CRD version, and Router/IPP versions; on failure return
  the missing items and a compatible version set rather than silently continuing.
- **Idempotency**: like the existing provisioning substeps, shared components that
  are already installed at the same version are not reinstalled; version changes
  upgrade as needed.

### 4.4 Step 4: Model Cache (optional)

Form fields (**corrected after requirement clarification: there is no
"automatic/default" behavior at all; the Storage portion is a fully manual form,
exactly the same as creating a volume on the Storage page**):

```
[x] Prewarm a model cache for this cluster now

── HuggingFace Token ──────────────────────────────────
Namespace    [dropdown listing all namespaces in this cluster]
Secret name  [hf-token-a1b2c3d4]  (defaults to a short-UUID suffix to avoid name
                                   collisions; editable;
                                   validation: no Secret with the same name may
                                   already exist in the selected namespace)
Token value  [__________________]  (password input)

── Model ────────────────────────────────────────────────
repo id      [meta-llama/Llama-3.1-8B-Instruct]
revision     [main]

── Storage location ────────────────────────────────────────
( ) Use an existing Storage volume: [dropdown showing only READY volumes under this
                                     cluster_id with kind=local-disk/nfs]
( ) Create a new Storage volume — the following is exactly the same as the Storage
    page's "New volume" form, with no default values; the user must fill out
    everything manually:
    kind          ( ) local-disk  ( ) nfs  ( ) ... (same as StorageVolumeCreateRequest)
    capacity      [__________]  (required, no default)
    hostPath      [__________]  (required when kind=local-disk, no default)
    nodeSelector  [__________]  (expand corresponding fields by kind; copied one
                                 by one from the existing form)
    readOnly      [ ]
    purposes      [ ] model-cache  [ ] ...
```

**Dependencies and implementation approach (redesigned after requirement
clarification):**

- **HF Token Secret is no longer a new `TokenSourceMode` inside Model Cache**.
  Instead, it is split into an independent, generic action: "create a Secret
  containing `HF_TOKEN` in the user-specified namespace". Add a new endpoint that
  is not attached to any specific Model Cache entry:

  ```
  POST /api/cluster/clusters/{cluster_id}/hf-token-secrets
  Body: HfTokenSecretCreateRequest { namespace, name?, token }
  Resp: HfTokenSecretRef { namespace, name }
  ```

  The implementation directly reuses the manifest structure of
  `deploy/runtime/composition.py::create_model_secret_from_token()`
  (`stringData: {HF_TOKEN: token}`), but changes it to explicitly
  **"create if absent, 409 if already exists"** (either by checking once with
  `kubectl get secret <name> -n <ns>` first, or directly using
  `kubectl create secret`—which already naturally means "error if exists", and
  avoids silent overwrite unlike the current Deploy logic with `kubectl apply`).
  The duplicate-name validation can reuse the same "list all secrets across
  namespaces" call as `cluster/service.py::list_model_secrets()`, or it can issue
  a direct precise `kubectl get secret <name> -n <namespace>` (more efficient;
  implementation can choose). The wizard's default name is
  `hf-token-<uuid4().hex[:8]>`; if the user edits it, validate again.
- After the Secret is created, the Model Cache entry continues to use the existing
  `TokenSourceMode.EXISTING_SECRET` (`namespace` / `name` pointing to the newly
  created Secret), fully reusing the mature existing path that already supports
  cross-namespace `copy_model_secret()`. **No new `TokenSourceMode` enum value is
  needed.** This is the key simplification versus the previous draft: the wizard
  simply "creates a Secret from the user's input first, then passes it into the
  Model Cache request as an existing Secret", so the semantics, security model,
  and persisted format all fully reuse the current implementation, with no new
  special branch to maintain.
- Storage volumes: there is **no automatic creation / no default values**. The
  user either selects an existing READY volume, or fills out the entire manual
  volume-creation form from the Storage page in the wizard
  (`StorageVolumeCreateRequest` copied verbatim, with fields, validation rules,
  and defaults—including the fact that there are no defaults—kept completely in
  sync with the Storage page, rather than inventing a second variant). The wizard
  background task calls `storage.service.StorageVolumeService.create()` under the
  `cluster_id` created in Step 1, obtains the resulting `volume_id`, and only
  continues after it becomes `READY`.
- Regardless of whether the user "chooses an existing volume" or "creates a new
  one", after the Storage volume is `READY`, the background task reuses the
  existing `ModelCacheService.request_download()` + `provision()`, exactly the
  same code path used when the user manually starts a download from the Model
  Cache page today. The only difference is that the initiator is the wizard's
  background task rather than a button click by the user.

### 4.5 Step 5: confirmation

Read-only summary of the choices in Steps 1-4 (skipped steps show as "not
enabled"), with a "Create cluster" button. If the cluster was already created
mid-wizard in Step 1 (see §4.1), clicking this step actually sends
`PATCH /api/cluster/clusters/{id}` (persist Step 2/3 config and start Step 4
background work), rather than another `POST`.

---

## 5. Provisioning state machine

```
                 ┌─────────┐
   (step not enabled) │ skipped │
                 └─────────┘

   (enabled)    ┌─────────┐    ┌─────────┐    ┌───────┐
                │ pending │ →  │ running │ →  │ ready │
                └─────────┘    └─────────┘    └───────┘
                                     │
                                     ▼
                                ┌────────┐
                                │ failed │ ──(user may retry individual substep)──┐
                                └────────┘                                      │
                                     ▲────────────────────────────────────────────┘
```

- The six substeps (`proxy-detection`, `storage`, `model-cache`, `llm-d-repo`,
  `llm-d-benchmark-repo`, `data-plane`) advance their states independently.
  `model-cache` depends on `storage` reaching `ready` first (if `storage` becomes
  `failed`, `model-cache` is marked `failed` directly, and `detail` explains the
  reason without duplicating the error). `proxy-detection` is non-`skipped` only
  when `mode="auto"`, and it is a best-effort probe: even if detection fails, the
  result is still marked `ready` +
  `detail="proxy not detected; proceeding without proxy"`; this **does not count
  as `failed`** (not being able to detect a proxy is a valid final state and
  should not delay/block completion of the wizard). `data-plane` is non-`skipped`
  only when a data-plane version set is selected in Step 3; on failure it keeps the
  already-installed parts and reports the missing items.
- `GET /api/cluster/clusters/{id}/provisioning` is used by the frontend for
  polling (reusing the polling interval conventions from the Model Cache page).
  Once all substeps enter terminal states (`skipped` / `ready` / `failed`), the
  frontend stops polling.
- An individual `failed` substep can be retried independently
  (`POST /api/cluster/clusters/{id}/provisioning/{step_id}/retry`) without
  rerunning the entire wizard—for example, if Model Cache download fails due to a
  transient network issue, the user retries only that part; "detect again" for
  `proxy-detection` reuses the same retry endpoint.
- The wizard modal can be minimized/closed before all substeps reach terminal
  states (closing the modal does not cancel background tasks), and the user can
  reopen "view creation progress" from the cluster list page at any time. This
  matches the user expectation already established by the Model Cache page, where
  closing the page does not stop the background download Job.

---

## 6. Summary of persistence layout changes

**What Provisioning status stores, and why it must be persisted (see the state
machine in §5):** Steps 2-4 are asynchronous background tasks, and the frontend
needs polling to know "which step is it on now, and did it succeed/fail/with what
detail"; that temporary record of "current status of each substep" is the
Provisioning status. If it lived only in memory (lost on process restart), the
frontend would suddenly receive "no such status" during polling, which users
would perceive as "cluster creation got stuck"; therefore it must be persisted.
The concrete location is the last row of the table below (**separate file**; see
the discussion in old §7 issue 2: store it separately from Cluster metadata to
avoid rewriting the entire Cluster record for every substep heartbeat, and to
avoid coupling it to the existing metadata read/write logic in `registry.py`).

| Data | Current path | Path in this proposal | Change |
|---|---|---|---|
| Cluster metadata | `<clusters_dir>/<id>.json` | Unchanged; add `proxy` / `llmDRef` / `llmDBenchmarkRef` / `dataPlane` fields (`proxy` includes `detected_*` / `detected_at` / `detection_error` detection snapshots) | Expanded |
| Storage volume records | `~/.cache/llm-d-bench/storage/volumes/*.json` | Unchanged (Step 4 creating a new volume calls the existing create logic, so the persistence location is unchanged) | None |
| HF Token Secret | No corresponding local record (the Secret itself lives in the target K8s cluster) | No new local record; only referenced through `TokenSourceMode.EXISTING_SECRET.namespace/name` inside the Model Cache entry | No new persistence |
| Model Cache entries | Existing `model_cache/store.py` path | Unchanged; continue using the existing `EXISTING_SECRET` branch, with no new enum value | None |
| llm-d repository cache | `~/.llm-d-bench/llm-d` (no version separation) | `~/.cache/llm-d-bench/repos/llm-d/<ref>` (new code path; old path remains as a fallback for Monitoring's current no-`ref` default scenario, with no forced migration) | New ref-based cache |
| llm-d-benchmark cache | `~/.cache/llm-d-bench/backends/<hash>` | Unchanged (not migrated in v1 of this proposal) | None |
| Data-plane version set (provider / GIE / Router / IPP) | None | Stored in the Cluster metadata `dataPlane` field; the actual components are installed into the target cluster (Helm charts / images), not cached as local source | New |
| **Provisioning status** | None | **New, standalone file `<clusters_dir>/<id>.provisioning.json`** (stored separately from Cluster metadata; even if the backend process restarts, the status of in-progress/completed substeps is preserved; after cluster creation fully finishes—all substeps in terminal state—it may optionally be cleaned up, or be retained permanently as a historical record of "what was done when creating this cluster") | New |
| **Application-level database config** (Step 0) | None | **New, standalone file `~/.llm-d-bench/db-bootstrap.json`** (`DatabaseBootstrapConfig`; because it must exist before the database does, it cannot be stored in the database itself, see §3.0/§4.0) | New |
| Storage medium of Cluster metadata **itself** | JSON file `<clusters_dir>/<id>.json` | Database table `clusters` (full field design in `sqlalchemy-data-access-layer-design.md` §5.4.1; migration inventory at line 12 of §7 in that doc). Before Step 0 completes, `registry.create_cluster()` is unavailable, and the wizard uses this to decide whether to show Step 0 | Storage medium changed (field structure unchanged; see §3.1) |

---

## 7. Confirmed design decisions (formerly open questions, all decided by the requester)

1. **Exact semantics of "use the cluster's own proxy":** **automatically detect it
   from the target cluster**, not "inherit the Lens backend process's environment
   variables". Implemented as a diagnostic Job detection flow; see §4.2.
2. **Where Provisioning status is stored:** standalone file
   `<clusters_dir>/<id>.provisioning.json` (last row in §6), not merged into
   Cluster metadata.
3. **Default capacity/hostPath for automatically created Storage volumes:** **no
   defaults at all**. The Storage portion of Step 4 is simply the manual
   create-volume form from the Storage page copied into the wizard, and the user
   must fill in all fields themselves (capacity, hostPath, etc.); the wizard does
   not provide a "one-click default" option.
4. **Secret namespace and naming for `TokenSourceMode.PASTE`:** namespace is
   chosen by the user in the wizard (dropdown listing all namespaces in that
   cluster); the default name is `hf-token-<uuid4().hex[:8]>` (short UUID to
   avoid collisions), and the user may change it, after which validate that "the
   same name must not already exist in that namespace" (reject with 409).
   **Architectural simplification:** do not add a `TokenSourceMode.PASTE` enum
   value; instead create the Secret first through a standalone endpoint, then let
   the Model Cache entry reference it using the existing `EXISTING_SECRET` mode
   (see §4.4).
5. **Whether Storage in Step 4 is "automatically created":** no. After the user
   manually completes the full form, the wizard calls the existing Storage create
   API—exactly the same code path as manually creating a volume on the Storage
   page. The wizard simply embeds that form into the flow to reduce user
   navigation.
6. **Whether database setup (Step 0) is a global one-time gate or configured per
   cluster:** **global, one-time**, not attached to `Cluster` records and not
   appearing in the step indicator / skip switch. Once the application has a
   configured database, it is permanently skipped; see §4.0.

---

## 8. Recommended phased rollout

- **Phase 0**: Step 0 (database setup gate page) + `llm_d_bench/db/`
  infrastructure (engine / session / migrations; see
  `sqlalchemy-data-access-layer-design.md`) + `clusters` table landing, and
  `registry.py` switched to DAO implementation. This is the prerequisite for all
  subsequent phases—Phase 1's Step 1 `create_cluster()` depends on the database
  already being available. It is recommended to schedule this together with the
  migration plan in §8 of `sqlalchemy-data-access-layer-design.md` (feature-flag
  rollout, legacy import script), to avoid maintaining both file storage and
  database storage code paths in parallel for too long.
- **Phase 1**: wizard UI shell (step indicator, skip logic) + Step 1 (unchanged)
  + the "custom proxy" branch of Step 2 (`mode="custom"` manually persists the
  three values + changes to `kubeconfig_environment()` / `_proxy_environment()`
  to read cluster-level configuration). **In Phase 1, the "automatic detection"
  branch (diagnostic Job) should remain disabled/hidden**, because it depends on
  new diagnostic Job infrastructure and has higher risk and implementation cost
  than the other steps; it should not block the immediately useful, simpler
  custom-proxy path from shipping first.
- **Phase 2**: the "automatic detection" branch of Step 2 (diagnostic Job +
  hostPath fallback + graceful degradation around known limitations) + Step 3
  (`repo_cache.py` extraction + `monitoring/cluster_stack/repo.py` support for a
  `ref` parameter + Evaluate-side "cluster default version" resolution priority).
- **Phase 3**: Step 4 (standalone HF Token Secret creation endpoint +
  `EXISTING_SECRET` reuse + embedded manual Storage form + Provisioning state
  machine + single-step retry). This step depends on the provisioning polling
  infrastructure from Phase 1 and has the largest scope, so it is recommended to
  land last.
- In every phase, when the user skips Steps 2-4 for the whole flow, behavior
  should remain exactly identical to today (this is the core regression-test
  assertion). For Phase 0, if the application already has a configured database,
  Step 0 should be completely invisible and should not change the experience of
  the current minimal flow.
