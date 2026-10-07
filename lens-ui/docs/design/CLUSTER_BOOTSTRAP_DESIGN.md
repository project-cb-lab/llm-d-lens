# Proposal: An End-to-End Flow from "Building a K8s Cluster" to "Creating a Lens Cluster"

- **Status:** Draft (v2, with §4 execution engine rewritten based on official-tool research results) — pending review.
- **Location:** `docs/design/CLUSTER_BOOTSTRAP_DESIGN.md`.
- **Reference architecture:**
  - Frontend: `src/components/CreateClusterWizard.jsx` (the existing multi-step “Create Cluster” wizard,
    see `cluster-creation-wizard-design.md`).
  - Backend: `llm_d_bench/cluster/` (registry/service/router, which manages metadata for registered
    clusters), `llm_d_bench/deploy/runtime/composition.py` (the “optional external executable”
    resolution pattern in `RuntimeEnvironment`, via `kubectl_path`/`helm_path`/`docker_path`
    — this proposal reuses the same pattern for `ansible-playbook`), and
    `llm_d_bench/utils/ssh.py` (existing paramiko-based remote execution utilities, currently unused;
    in this proposal they are downgraded to lightweight preflight connectivity probing, while actual
    deployment work is delegated to the official tools researched below).

---

## 0. Research conclusion (answering "is there an official tool" first)

The Kubernetes official documentation, [Installing Kubernetes with deployment tools](https://kubernetes.io/docs/setup/production-environment/tools/),
explicitly lists four official/SIG-recognized cluster deployment tools. The following evaluates them
one by one for this proposal's scenario (**the user provides several networked bare-metal or virtual
machines that can be reached over SSH, with no cloud-provider API available**):

| Tool | Ownership | Suitable scenario | Suitable for this proposal? |
|---|---|---|---|
| **kubeadm** | Kubernetes core subproject | Single-node primitive-level init/join commands; by itself it does not solve “multi-node orchestration”, “retry semantics”, or “CNI selection”. The previous draft treated kubeadm as a primitive and hand-wrote SSH orchestration around it, effectively reinventing wheels already built and production-validated by Kubespray/kOps | Use it as a **dependency** (Kubespray also uses kubeadm underneath), but do **not** expose it directly to users |
| **[Cluster API](https://cluster-api.sigs.k8s.io/) (CAPI)** | Kubernetes SIG-cluster-lifecycle subproject | Declarative, designed for multi-cluster lifecycle management, but requires a **management cluster** first (typically bootstrapped with `kind`/`k3s`) plus an Infrastructure Provider for the target platform (for bare metal, Metal3/CAPM3, which depends on BMC/Redfish/PXE bootstrapping and out-of-band management) — far heavier than this proposal's assumption of “only SSH credentials” | Not suitable for MVP; reserve for Phase 2+ (see §8), and reconsider only if future node inventory includes BMC |
| **[kOps](https://kops.sigs.k8s.io/)** | Kubernetes SIG-cluster-lifecycle subproject | Historically focused on cloud-provider automation (primarily AWS, secondarily GCE/Azure) to create full cloud resources such as VPCs, LBs, and ASGs. Although its docs include [Bare Metal](https://kops.sigs.k8s.io/getting_started/bare_metal/) support, its maturity and community usage are clearly weaker than in cloud scenarios | Not suitable (this proposal has no cloud-provider API available, and bare metal is a secondary scenario for it) |
| **[Kubespray](https://kubespray.io/)** | Kubernetes SIG-cluster-lifecycle subproject | A set of **Ansible playbooks**. Its official README explicitly states support for **Bare Metal**; it only requires SSH + sudo; the input is an Ansible inventory (which maps almost exactly to the “node list” this proposal needs to collect in §3); it supports HA multi-control-plane, selectable CNI (default **Calico**, matching existing precedent in the current environment), and major Linux distributions including **Ubuntu/Debian/RHEL family (CentOS Stream, RHEL, Rocky, AlmaLinux, Oracle Linux) / Fedora / openSUSE** (details in §3.2). It has extensive built-in preflight checks, and `cluster.yml`/`scale.yml`/`remove-node.yml` correspond directly to “create cluster”/“add node”/“remove node”. It is **idempotent and rerunnable** (if one node fails, fix it and rerun the same playbook; there is no need to wipe everything and start over) | ✅ **Chosen for this proposal** |

**Conclusion: do not reinvent kubeadm orchestration logic.** Replace the hand-written kubeadm+SSH
state machine in §4 “Bootstrap execution engine” of the previous draft with **Kubespray**.
Lens only needs to: ① render the node list collected by the wizard into the inventory format
expected by Kubespray, ② run `ansible-playbook cluster.yml` as a subprocess, and ③ read the
`admin.conf` produced by Kubespray itself.
This delegates the most error-prone and maintenance-heavy part of the proposal (retry semantics for
multi-node joins, CNI installation, per-distribution dependency differences) wholesale to an
official tool already validated at large production scale.

---

## 1. Background and goals

### 1.1 Current state

Step 1 (“Basic information”) of `CreateClusterWizard.jsx` assumes the user **already** has a usable
Kubernetes cluster and already has a `kubeconfig` file in hand. The user uploads it, and only then
can Lens register the cluster into `llm_d_bench/cluster/registry.py` (writing `<id>.json` +
`<id>.kubeconfig`).

For a benchmark engineer who has just obtained several bare-metal machines (or several VMs that do
not yet have Kubernetes installed), there is still an entire unsupported gap between “bare metal”
and “a cluster ready to run llm-d”. They have to manually follow kubeadm tutorials and debug each
machine's dependencies and networking issues on their own, completely disconnected from Lens. When
something goes wrong, Lens has no visibility at all.

### 1.2 Goal

Insert a new segment in front of the existing “Create Cluster” wizard entry so that the user can
choose:

- **Path A (current behavior):** I already have a cluster; upload kubeconfig directly — completely
  unchanged. The wizard starts from Step 1 as today; see
  `cluster-creation-wizard-design.md`.
- **Path B (new):** I only have several machines and no Kubernetes yet — Lens uses **Kubespray** to
  install a production-usable cluster on those machines, then automatically feeds the kubeconfig
  produced by Kubespray into Step 1 so the existing wizard continues seamlessly (proxy/version/Model
  Cache and all later steps remain unaffected).

The core constraint of the design is: **Path B only inserts one new step in front of Path A that
produces a kubeconfig string; it does not modify any Step 1–6 code/API in the existing wizard.**
This minimizes implementation and review cost for the proposal and also ensures the existing minimal
“upload kubeconfig only” flow and existing automation scripts remain unaffected.

### 1.3 Non-goals

- No integration with cloud-provider managed Kubernetes provisioning APIs (GKE/EKS/AKS, etc.) —
  those are entirely different authentication/billing/resource models; although kOps officially
  supports them, they are outside the scope of this proposal.
- No Cluster API/Metal3 — that requires BMC/PXE out-of-band management and is far heavier than the
  “only SSH credentials” assumption here; leave it for Phase 2+ (see §8).
- No operating-system-level node initialization (partitioning, OS installation, network setup) —
  assume the user-provided machines are already reachable by SSH using an account with sudo, and can
  reach the Internet or an internal image mirror. Kubespray itself handles retrieval of containerd,
  Kubernetes component packages, and CNI images; for offline image schemes see
  [Kubespray's Air-Gap documentation](https://github.com/kubernetes-sigs/kubespray/blob/master/docs/operations/offline-environment.md).
  MVP assumes nodes can directly reach the Internet or the company's proxy and does not implement
  offline image distribution.
- No CNI selector beyond Kubespray — MVP fixes Kubespray's default Calico; however, **the OS support
  matrix is aligned directly with the Kubespray official support list** (see §3.2), rather than
  artificially narrowing it to a single distribution matching the local development environment.

---

## 2. User flow (the split point between the two paths)

At the top of Step 1 in `CreateClusterWizard.jsx`, add a two-option entry point (it is not part of
`STEPS`; it is a pre-branch inside Step 1):

```
┌───────────────────────────────────────────────────┐
│  This cluster is…                                 │
│  ○ I already have kubeconfig (current behavior)   │
│  ○ I only have a few machines; help me install    │
│    Kubernetes (new, based on Kubespray)           │
├───────────────────────────────────────────────────┤
│  [Expanded node list + Bootstrap progress view    │
│   after selecting the second option]              │
└───────────────────────────────────────────────────┘
```

After selecting the second option, Step 1 temporarily switches into a “sub-wizard” (reusing the UI
in §4). Once it finishes and obtains the kubeconfig string, it is **automatically backfilled** into
where Step 1 originally expected an uploaded kubeconfig file (displayed as a read-only “upload
succeeded” state, with an option to “choose again” and return to manual upload). The user then
clicks “Next” as normal and proceeds into the existing Step 2 (proxy) through Step 6 (confirmation).
To the rest of the wizard, this auto-generated kubeconfig is no different from a manually uploaded
file.

---

## 3. Node inventory and preflight validation

### 3.1 Inputs

The first screen of the sub-wizard collects a node table (not just a pile of form fields; model it
on the node selector interaction in `StorageManagement`). Its fields correspond directly to the
minimum data Kubespray needs for a single host in the inventory:

| Column | Description | Corresponding Kubespray inventory field |
|---|---|---|
| Host | One row may contain **a single IP/hostname**, **multiple IPs/hostnames separated by commas**, or **an IP range** (short form `10.0.0.10-20` means the last octet runs from 10 to 20; it also supports full ranges across subnets such as `10.0.0.250-10.0.1.5`). The range is expanded in the backend into multiple independent nodes, all inheriting the same checked Role(s) and authentication method from that row. A single expansion is capped at 256 nodes; exceeding that, or an invalid range itself (e.g. start/end reversed or field overflow), causes a submission-time error. Strings containing `-` that do not form a valid range (such as hostname `worker-1.internal`) are treated as literal hostnames and do not error | After expansion, each host's own `ansible_host` |
| SSH Port | Default 22 | `ansible_port` |
| Role(s) | **Checkboxes**; `control-plane` and `worker` may both be selected (at least one node must select `control-plane`; MVP allows multiple control-plane nodes for HA — Kubespray supports this natively, unlike the previous draft which artificially restricted to a single control plane. **A node may simultaneously be both control-plane and worker** — very common for single-node validation or small clusters. In implementation, that means the same host appears in both Kubespray groups `kube_control_plane` and `kube_node`; they are not mutually exclusive) | Selecting `control-plane` puts it into `kube_control_plane` (and also `etcd`); selecting `worker` puts it into `kube_node`; selecting both enters both groups |
| Authentication method | Paste private-key text, or username + password | `ansible_user` + `ansible_ssh_private_key_file` (for password mode, use `ansible_ssh_pass`, which requires `sshpass` on the target side of execution) |

At least one node must be marked `control-plane`. It may or may not also be marked `worker`
(single-node validation/development scenario, corresponding to Kubespray's all-in-one topology —
with only one node, check both `control-plane` and `worker`, and it will appear in all three groups
`kube_control_plane`/`kube_node`/`etcd`).

The pasted private key is written into a temporary file that exists only for the lifecycle of the
current Bootstrap Job (`0600`), and is immediately `unlink`ed on success/failure. It is never
written into the persistent Cluster directory. It is only used for
`ansible-playbook --private-key=<temporary-file>`.

**Network proxy on target nodes:** below the node table there is also an independent “Network proxy
for target nodes” switch (`auto`/`custom`), rendered into Kubespray's
`group_vars/k8s_cluster/k8s-cluster.yml` (`http_proxy`/`https_proxy`/`no_proxy`, which are
officially recognized Kubespray variables; see its
`inventory/sample/group_vars/all/all.yml`). This is distinct from Step 2 “Network Proxy” (which only
affects the Lens backend itself when pulling the llm-d/llm-d-benchmark repositories). Here it
controls **the target nodes themselves** when they run OS package manager installs, and when roles
such as `container-engine/runc` download binaries directly from GitHub. If the target nodes lack a
direct route to the public Internet (common in enterprise proxy environments), failing to set these
variables causes errors like `ansible-playbook exited with status 2`, often with messages similar
to `container-engine/runc : Download_file | Download item` repeatedly retrying and then failing.
`auto` (default) probes each target node via SSH for its already-configured proxy settings (reading
`/etc/environment` and exported `*_proxy`/`*_PROXY` variables after preflight succeeds but before
Kubespray is actually invoked), takes the first non-empty `http_proxy`/`https_proxy` found across
the nodes, and uses the union of all nodes' `no_proxy`. If none of the target nodes themselves have
proxy settings, it is treated as “no proxy needed” (it will not fall back to the Lens backend
process's own environment variables; the two may be in completely different networks). `custom`
allows specifying proxy settings for this batch of target nodes directly, and they take effect
immediately on submission (no probing required).

**`no_proxy` auto-completion (important):** Kubespray itself **does not** automatically append any
cluster-internal addresses to `no_proxy` — its default is to pass through exactly what the caller
provides (see `roles/kubespray_defaults/defaults/main/main.yml`:
`no_proxy: "{{ no_proxy | default('') }}"`). This means that once a node has `http_proxy`/
`https_proxy`, **all** its HTTPS traffic — including `kubectl`/kubelet/etcd access to
`127.0.0.1:6443` or peer node IPs — will be sent to the enterprise proxy. Proxies usually reject
this kind of internal direct-connect traffic, which manifests as a cluster that finished
installation but then `kubectl get pods -A` suddenly reports
`Unable to connect to the server: Forbidden` (HTTP 403 from the proxy, not an actual inability to
reach the API server). Therefore, when a proxy is configured (whether `auto` or `custom`),
`write_inventory`/`inventory.py::_augment_no_proxy` automatically merges the following into
`no_proxy` (deduplicated while preserving user-supplied values): `localhost,127.0.0.1`, the
Kubespray defaults when not overridden for `kube_service_addresses`/`kube_pods_subnet`
(`10.233.0.0/18` / `10.233.64.0/18`),
`kubernetes,kubernetes.default,kubernetes.default.svc,kubernetes.default.svc.cluster.local,.svc,.svc.cluster.local,.cluster.local`,
and each target node's own host/IP from this submission.

### 3.2 Preflight checks (per-node, concurrent, the fast-fail layer before Kubespray)

Kubespray's own `bootstrap-os`/`preinstall` roles already include many preflight checks, but those
only show up when the playbook actually starts running (possibly after several minutes). To let the
user know within seconds of finishing the node list that “this machine is unreachable / lacks sudo”,
clicking “Next” (not “Finish”) first runs a read-only quick check using
`llm_d_bench/utils/ssh.py::remote_exec`. Only after all checks pass may the user proceed to the next
screen and trigger Kubespray for real:

- SSH reachable + authentication succeeds (`remote_exec(target, "true")`).
- Passwordless sudo via `sudo -n true` works (Kubespray requires root throughout, corresponding to
  `ansible-playbook -b`).
- The operating system is in Kubespray's supported list (`ID`/`VERSION_ID` from `/etc/os-release`,
  aligned with [Kubespray v2.31.0 README “Supported Linux Distributions”](https://github.com/kubernetes-sigs/kubespray/blob/v2.31.0/README.md#supported-linux-distributions),
  rather than the narrower set represented by the local development environment. Currently included:
  Ubuntu 22.04/24.04, Debian 11/12/13, CentOS Stream/RHEL 9/10, Fedora 39-42, openSUSE Leap
  15.x/Tumbleweed, Oracle Linux 9/10, AlmaLinux 9/10, Rocky Linux 9/10. Distributions Kubespray
  marks as “experimental” — Flatcar, Fedora CoreOS, Kylin, UOS, openEuler, Amazon Linux 2 — are
  excluded from MVP because they require additional distro-specific bootstrap steps.)
- **The node must not already have Kubernetes deployed** — read-only checks on
  `/etc/kubernetes/admin.conf`, `/etc/kubernetes/manifests/kube-apiserver.yaml`, whether `kubelet`
  is running, and whether the `kubeadm` command exists. If any condition matches, the node is
  judged to “already have a Kubernetes installation”, and the request is rejected immediately with a
  prompt to run `kubeadm reset` first (or switch to a clean node). Running `cluster.yml` again on an
  already-existing cluster is not allowed, to avoid kubelet/etcd/certificate conflicts causing
  unpredictable state.

If any node fails this preflight layer, only **that node** is blocked. The user may fix its
configuration in place and retry, or remove it from the list and continue, but **at least one
`control-plane`** must pass to proceed. After this fast-fail layer, finer-grained dependency and
kernel-parameter checks are left to Kubespray's own `preinstall` role (no need to reinvent them).

---

## 4. Bootstrap execution engine (based on Kubespray)

### 4.1 Integrating Kubespray as a “managed external dependency”

Following the repository's existing two patterns for vendoring external artifacts, choose one of
the following (implementation can decide later; both are viable):

- **Mode A (recommended; same shape as the llm-d / llm-d-benchmark repo caches):** like
  `llm_d_bench/monitoring/cluster_stack/repo.py` (clone into `~/.llm-d-bench/llm-d`) and the
  Evaluate-side `_managed_benchmark_root()` (cache repo+revision hash into
  `~/.cache/llm-d-bench/backends/<hash>`, then clone → checkout → create venv → `pip install`, with
  a file lock preventing concurrent duplicate installs), add
  `llm_d_bench/cluster/bootstrap/kubespray_repo.py`: clone
  `https://github.com/kubernetes-sigs/kubespray` at a fixed tag (for example `v2.31.0`, the newest
  README-labeled version seen during research) into `~/.cache/llm-d-bench/kubespray/<tag>`, and
  create a dedicated venv using that checkout's `requirements.txt` to install Ansible (Kubespray is
  tightly bound to specific Ansible versions, so reusing system Ansible is not acceptable).
- **Mode B (lighter weight):** directly use Kubespray's official container image
  `quay.io/kubespray/kubespray:v2.31.0` (exactly as shown in the README Docker Quick Start). The
  Lens backend uses `docker run` to mount the generated inventory directory plus the temporary
  private-key file, and runs
  `ansible-playbook -i /inventory/inventory.ini --private-key /root/.ssh/id_rsa cluster.yml`
  inside the container.
  The benefit is that there is no need to manage a Python venv/Ansible version; the downside is
  that the Lens backend host itself must have Docker. (`RuntimeEnvironment` already resolves
  `docker_path` in `composition.py`, so this prerequisite is not a new burden in this repository.)

The **integration shape** for both modes is identical: “take a rendered inventory directory, run a
subprocess, and produce an `admin.conf`”; the only difference is how that subprocess is invoked.
At implementation time, use the same “optional external executable path resolution” pattern as
`RuntimeEnvironment` (`llm_d_bench/deploy/runtime/composition.py`), with an added
`ansible_playbook_path` / `kubespray_docker_image` configuration item.

### 4.2 Phases (the state machine, much simpler than the previous draft — multi-node join, CNI
installation, and HA are all handled inside Kubespray; Lens only observes Ansible execution
progress)

```
queued → preflight (§3.2, done by Lens itself) → [provisioning (only on first run; see below)]
       → running (`ansible-playbook cluster.yml` subprocess) → kubeconfig_ready → succeeded
                                                                  ↘ failed (non-zero subprocess exit, carrying stdout/stderr tail)
```

- **`provisioning` (automatic Kubespray provisioning):** `kubespray.require_ready()` only checks;
  when the local cache (`~/.cache/llm-d-bench/kubespray/<tag>`) is missing, **operations no longer
  need to manually run `ensure_ready()` in advance**. Instead, the Job itself enters the
  `provisioning` phase and calls `kubespray.ensure_ready()` (clone the specified tag + create a
  dedicated venv using its bundled `requirements.txt`). On success it continues into `running`; on
  failure (no network, clone/pip error, etc.) it goes directly to `failed`, returning the original
  error to the user. Only the **first Bootstrap Job on the same backend process** passes through
  this phase; later jobs reuse the cached checkout and skip `provisioning`. Because this involves a
  real `git clone` / `pip install` (which may take minutes), it should be offloaded into a thread
  pool via `asyncio.to_thread` so the event loop is not blocked.

- **Render inventory:** convert the node list collected in §3.1 into the YAML inventory required by
  Kubespray (`all.hosts.<name>.ansible_host/ansible_port/ansible_user/...` + the three child groups
  `kube_control_plane` / `kube_node` / `etcd`) and write it to
  `<bootstrap_job temporary directory>/inventory.yml`.
- **group_vars overrides:** write a minimal `group_vars/k8s_cluster/k8s-cluster.yml` override that
  locks three things:
  `kube_network_plugin: calico` (although this is Kubespray's default, write it explicitly so a
  future Kubespray default change does not silently change this proposal's behavior),
  `kubeconfig_localhost: true` + `kubectl_localhost: true` (critical — these make Kubespray output
  kubeconfig directly into `inventory/<job>/artifacts/admin.conf` on the **machine running
  Ansible** — i.e. the Lens backend itself — eliminating the entire extra step from the previous
  draft of “SSH back into the control plane and read `/etc/kubernetes/admin.conf`”), and
  `override_system_hostname: false` (Kubespray defaults this to `true`, which changes the real
  `/etc/hostname` on each target machine to the node name in the inventory. For machines that
  already have hostnames assigned by ops/IT, this is unnecessary and surprising, so we explicitly
  disable it. Kubespray still uses the inventory names for internal grouping/addressing, and this
  does not affect whether the cluster itself starts correctly).
- **running:** start
  `ansible-playbook -i inventory.yml -b --private-key=<temporary private key> cluster.yml`
  asynchronously (or the equivalent `docker run` form for Mode B in §4.1), read stdout line by line
  and incrementally feed it into the Bootstrap Job's log buffer (reusing the `_MAX_OUTPUT` tail
  truncation pattern), and let the frontend poll and display “last N lines of Ansible output” as a
  progress indicator. Kubespray's Ansible output is already grouped by task, so Lens does not need
  to parse finer-grained phases such as “what exact stage is it on now” — another “free” capability
  obtained by using a mature tool.
- **kubeconfig_ready:** after the subprocess exits with code 0, read the contents of
  `inventory/<job>/artifacts/admin.conf` as the kubeconfig text.
- **failed:** when the subprocess exits non-zero, the Bootstrap Job records the stdout/stderr tail
  (Ansible failures usually identify the specific host and task that failed, allowing the user to
  tell whether this was a network issue or an environment problem on a particular machine).

### 4.3 Adding/removing nodes (mapping to Kubespray's `scale.yml` / `remove-node.yml`)

Not in MVP scope (see §8), but worth documenting: because Kubespray was chosen, the future need of
“add/remove worker nodes for an already registered Lens cluster” does not need a fresh design.
As long as Cluster metadata retains an optional marker such as “was this cluster created by Lens
using Kubespray, and which inventory corresponds to it” (for example
`bootstrapProvider: "kubespray"` + inventory directory path), the Storage/Monitoring pages could
later add a “scale out” button that simply rerenders the inventory (adding the new nodes) and runs
`scale.yml`. This is an immediately visible compounding benefit of choosing an official tool rather
than hand-writing kubeadm orchestration.

### 4.4 Data model (in-memory only, not persisted to Cluster registry)

A Bootstrap Job is **not** a variant of `llm_d_bench/cluster/registry.py::Cluster`; its lifecycle
ends once the kubeconfig string is obtained:

```python
@dataclass
class BootstrapNode:
    host: str
    # Non-empty list; one node may hold both control-plane and worker roles
    # simultaneously (see §3.1).
    roles: list[Literal["control-plane", "worker"]]
    preflight: Literal["pending", "passed", "failed"]
    preflight_error: str | None = None


@dataclass
class BootstrapJob:
    id: str  # uuid4
    phase: Literal["queued", "preflight", "provisioning", "running", "kubeconfig_ready", "succeeded", "failed"]
    nodes: list[BootstrapNode]
    inventory_dir: Path  # temporary dir, containing inventory.yml + group_vars + artifacts/
    log_tail: str  # truncated tail of ansible-playbook output
    kubeconfig_text: str | None = None  # only present on succeeded, one-time consumption
    created_at: str
```
Store it in a pure in-memory `dict[str, BootstrapJob]`, following the pattern of
`llm_d_bench/cluster/sessions.py::ClusterSession` (lost on process restart — acceptable, because
Bootstrap is inherently a one-shot operation; if interrupted, the user can just initiate it again).
`inventory_dir` is cleaned up once the Job reaches a terminal state (`succeeded`/`failed`) and the
frontend has read the kubeconfig/logs.

---

## 5. API surface

Add a set of endpoints independent from the existing cluster CRUD routes in
`llm_d_bench/cluster/router.py`:

| Method & path | Description |
|---|---|
| `POST /api/cluster/bootstrap/preflight` | Takes the node list, synchronously runs the quick checks in §3.2, and returns per-node pass/fail details. |
| `POST /api/cluster/bootstrap` | Called after all preflight checks pass: renders the inventory, starts a background task running `ansible-playbook cluster.yml`, and returns `{ bootstrapId }`. |
| `GET /api/cluster/bootstrap/{id}` | Polls status + log tail; when `phase == "succeeded"`, the response body also includes `kubeconfig` (returned only once; after it is read, the field is cleared from the in-memory Job and `inventory_dir` is cleaned up). |
| `POST /api/cluster/bootstrap/{id}/cancel` | If the user closes the sub-wizard mid-run: terminate the running `ansible-playbook` subprocess (`asyncio.subprocess` `.terminate()`, following the existing Helm subprocess management pattern in `precise_prefix_cache_routing.py`). |

Existing Cluster DTOs such as `ClusterSettingsUpdateRequest` remain **unchanged**.

---

## 6. Security considerations

- **SSH credentials only flow through memory/temporary files:** private keys/passwords are supplied
  once in the request body for one-time use. The private-key file is written with strict `0600`
  permissions and deleted immediately when the Job ends, whether successfully or not.
  For password-authenticated nodes, the password **is not** written into the shared `inventory.yml`
  (that file itself is not permission-hardened and is easy to leak through `cat` or logging).
  Instead, `render_host_vars()` generates a separate `host_vars/<host>.yml` for each password-based
  node (`{ansible_ssh_pass: ...}`), written under `work_dir/host_vars/` with `0600` permissions as
  well. Ansible automatically loads `host_vars/` from the inventory directory, so there is no need
  for extra `--extra-vars` or command-line arguments (which are visible to other users on the same
  host via `ps`). The Job's `finally` block deletes the entire `host_vars/` directory together with
  the private-key temp directory via `rmtree`, preventing plaintext passwords from remaining on
  disk.
  In addition, password authentication depends on Ansible's `ssh` connection plugin invoking the
  `sshpass` binary on the Lens **backend** host, not on the target nodes. Therefore, before starting
  a Job, the backend checks whether `sshpass` exists (`shutil.which`); if it does not, fail fast and
  tell the user to install `sshpass` on the Lens backend host instead of letting Ansible fail
  halfway through. Logs/`log_tail` must be redacted before being stored into `BootstrapJob`
  (verbose Ansible output may echo variables such as `ansible_ssh_pass`; filter out lines
  containing those field names while reading subprocess output, or rely on `no_log: true` where
  Kubespray already does so in its built-in tasks).
- **One-time kubeconfig delivery:** once `GET /api/cluster/bootstrap/{id}` returns kubeconfig, the
  field is immediately cleared from the in-memory Job and `artifacts/admin.conf` is deleted from
  disk, preventing the same sensitive file from lingering on the Lens backend host.
- **sudo boundary:** `ansible-playbook -b` (`become`) lets Kubespray decide which tasks require
  privilege elevation, making it more reliable than the previous draft's hand-written logic of
  which commands should or should not use sudo. Kubespray already contains community-maintained
  logic covering many edge cases.
- **Host-key verification:** Before the first authenticated connection, the UI calls
  `POST /api/cluster/bootstrap/host-keys` to retrieve each expanded host's SSH public-key SHA-256
  fingerprint without credentials. It displays the `host:port`, algorithm and fingerprint, and
  requires the operator to confirm an independent check against a trusted source. Both preflight
  and job creation require `hostKeys` pins covering every expanded node; the backend rejects
  missing, extra or duplicate pins. Paramiko accepts only an exactly matching server key before
  authentication. Before Ansible runs, the backend checks the key again and writes a per-job
  `known_hosts` file with `0600` permissions. `ANSIBLE_SSH_COMMON_ARGS` sets
  `StrictHostKeyChecking=yes`, `UserKnownHostsFile`, and an isolated global known_hosts file.
  If the key changes, the job fails and requires renewed verification. Temporary files are removed
  when the job ends. **Displaying a fingerprint and checking a box does not establish trust:**
  the operator must verify it through an independent trusted channel, such as a cloud console,
  asset inventory or on-site records. Otherwise initial discovery remains exposed to a
  man-in-the-middle attack; connection-time pinning prevents replacement after confirmation.

---

## 7. Failure handling and retries

- Kubespray's `cluster.yml` is inherently designed to be **idempotent and rerunnable**. If one run
  fails because of a transient network issue on a particular machine, Lens does not need to
  implement a fine-grained state machine like “retry this stage on that node” (which occupied a long
  section in §7 of the previous draft). After a Bootstrap failure, if the user clicks “Retry”, Lens
  only needs to rerun `cluster.yml` with the same inventory. Nodes/steps that already succeeded are
  automatically detected by Ansible as “already satisfied; skip”. This is a **clear complexity
  reduction** gained by using a mature tool: this section is much shorter than the v1 draft because
  most of the complexity no longer belongs in Lens.
- The only retry semantic Lens itself needs to handle: if the user **changes the node list** before
  retrying (for example, removing a persistently unreachable worker), Lens must rerender the
  inventory and rerun, rather than simply retrying the old inventory.

---

## 8. Non-goals / Phase 2+ (reserved for future proposals)

- Cluster API + Metal3 (bare metal requires BMC/PXE out-of-band management) — see the research
  conclusion in §0; reconsider only when future node inventory truly includes out-of-band control.
- Cloud-provider managed Kubernetes (GKE/EKS/AKS) or kOps cloud automation — completely different
  API/billing/IAM models.
- CNI choice (Cilium/Flannel/kube-ovn, etc.; Kubespray itself supports them, but MVP does not
  expose that choice in the UI).
- Operating-system-level node initialization (OS install, partitioning, NIC configuration) — assume
  the machines are already SSH-accessible.
- Offline/Air-Gap image distribution (Kubespray has documentation for it, but it introduces
  substantial UI and storage complexity, so postpone it until a real offline deployment need
  appears).
- The “scale an already registered cluster” idea mentioned in §4.3 — finish the “create a cluster
  from zero to one” path first, then treat scaling as a high-reuse follow-on proposal.
- Preserving Bootstrap Job state across process restarts (currently pure in-memory; restart loses
  it and the user must restart the action — even so, because Kubespray is idempotent, the cost of
  restarting is still far lower than in the v1 draft).

---

## 9. Implementation plan (suggested milestones)

1. **M1 — Backend skeleton + Kubespray integration validation:** first, outside Lens UI, manually
   validate either Mode A or Mode B from §4.1 on a test machine, confirming that Kubespray can
   correctly pull containerd/Kubernetes component packages/Calico images under this machine's proxy
   environment (`http_proxy=http://proxy-ir.intel.com:912`, from the actual environment variables
   found in earlier sessions). This is the **largest unknown risk** in the proposal and must be
   manually proven before writing Lens orchestration code.
2. **M2 — inventory rendering + backend API:** add the `llm_d_bench/cluster/bootstrap/` module
   (`models.py` / `inventory.py` / `service.py` / `router.py`), subprocess management, and log-tail
   truncation. Unit tests should cover correctness of inventory rendering (no real machines needed;
   only verify “given this node list, does the rendered YAML match Kubespray inventory schema”).
3. **M3 — frontend sub-wizard:** Step 1 pre-branch UI in `CreateClusterWizard.jsx` + progress view
   (showing the tail of Ansible output), wired to the M2 API.
4. **M4 — security hardening:** implement verified host-key pinning and the per-job `known_hosts` file from §6 and code-review
   the use of an `extra-vars.yml` file to pass arguments (avoiding plaintext credentials on the command line), and handling of `artifacts/admin.conf` cleanup after it is read.

---

## 10. Open questions (need review confirmation)

1. In §4.1, choose Mode A (vendor Kubespray git checkout + dedicated venv) or Mode B (directly use
   the official `quay.io/kubespray/kubespray` container image)? The latter requires Docker on the
   Lens backend host; the former requires that Kubespray's fixed-version `requirements.txt` can be
   `pip install`ed without conflicting with this repository's other Python dependencies.
2. Should HA multi-control-plane (which Kubespray natively supports and §3.1's role design already
   reserves for) be available in MVP, or should MVP initially allow only one control-plane node and
   defer HA until users explicitly demand it? (The llm-d benchmark scenario itself does not have a
   strong control-plane high-availability requirement. For one-off benchmark clusters, rerunning
   Bootstrap after a single control-plane failure may be more cost-effective than carrying the added
   operational complexity of HA — the current lean is to allow only one node in MVP, but this needs
   review confirmation.)
3. Is the temporary known_hosts collection strategy mentioned in §6 acceptable, or is a stricter
   scheme required (for example, asking the user to provide each machine's SSH host-key fingerprint
   when filling out the node list, so Lens verifies it before connecting and removes all reliance on
   “trust on first use”)?
