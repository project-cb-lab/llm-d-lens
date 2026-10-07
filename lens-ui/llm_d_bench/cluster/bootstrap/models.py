"""In-memory job/state models for the Kubespray bootstrap flow.

See ``docs/design/CLUSTER_BOOTSTRAP_DESIGN.md`` section 4.4. Nothing here is persisted to
disk beyond the ephemeral working directory a job uses while it runs; the
job store itself is a plain in-process dict, mirroring
``llm_d_bench.cluster.repo_downloads``'s in-memory status pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

NodeRole = Literal["control-plane", "worker"]
PreflightState = Literal["pending", "running", "passed", "failed"]
JobPhase = Literal[
    "queued",
    "preflight",
    "provisioning",
    "running",
    "kubeconfig_ready",
    "succeeded",
    "failed",
    "cancelled",
]

#: Kept small; the frontend only ever needs "the last screenful" of Ansible
#: output to show progress, not a full transcript.
_LOG_TAIL_MAX_CHARS = 32_000


@dataclass(frozen=True)
class PreflightCheckItem:
    """One named sub-check within the overall per-node preflight (design
    §3.2) -- surfaced to the UI so users can see *what* was checked and
    *why* a node failed, not just an opaque pass/fail."""

    id: str
    label: str
    status: Literal["passed", "failed", "skipped"]
    detail: str | None = None


@dataclass
class BootstrapNode:
    host: str
    port: int = 22
    #: Non-empty; a node commonly holds both roles at once (all-in-one /
    #: small clusters where the control-plane is also schedulable) -- see
    #: docs/design/CLUSTER_BOOTSTRAP_DESIGN.md. Kubespray just needs the host listed in
    #: both the ``kube_control_plane`` and ``kube_node`` groups for that.
    roles: list[NodeRole] = field(default_factory=lambda: ["worker"])
    username: str = "root"
    # Exactly one of these two is expected to be set by the caller; never
    # persisted anywhere outside this in-memory dataclass.
    private_key: str | None = None
    password: str | None = None
    host_key_fingerprint: str | None = None
    preflight_state: PreflightState = "pending"
    preflight_error: str | None = None
    #: The full breakdown of individual preflight sub-checks (SSH
    #: reachability, passwordless sudo, supported OS, no pre-existing K8s
    #: install) run against this node, in the order they were run -- see
    #: ``preflight.py::_run_check``.
    preflight_checks: list[PreflightCheckItem] = field(default_factory=list)
    #: Populated by the service layer (see ``service.py::_materialize_keys``)
    #: once ``private_key`` has been written to a ``0600`` temp file; used by
    #: both the preflight SSH check and the rendered Ansible inventory.
    private_key_path: str | None = None

    @property
    def name(self) -> str:
        """A safe-for-inventory node name (Kubespray host alias)."""
        return self.host.replace(".", "-").replace(":", "-")

    @property
    def is_control_plane(self) -> bool:
        return "control-plane" in self.roles

    @property
    def is_worker(self) -> bool:
        return "worker" in self.roles


@dataclass
class BootstrapJob:
    id: str
    nodes: list[BootstrapNode]
    phase: JobPhase = "queued"
    log_tail: str = ""
    error: str | None = None
    kubeconfig_text: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    #: Working directory holding the rendered inventory / group_vars /
    #: artifacts (including Kubespray's ``admin.conf`` once produced) plus
    #: any temporary private-key file. Removed once the job reaches a
    #: terminal phase and its kubeconfig/log have been consumed.
    work_dir: Path | None = None
    #: Populated once the ``ansible-playbook`` subprocess has been spawned,
    #: so a cancel request can terminate it.
    process_pid: int | None = None
    cancelled: bool = False
    #: Resolved ``{"http_proxy": ..., "https_proxy": ..., "no_proxy": ...}``
    #: (any key may be absent) to export on the target nodes -- see
    #: ``dto.BootstrapProxyConfig`` / ``inventory.render_group_vars``. For
    #: ``proxy_mode="custom"`` this is filled in immediately at job
    #: creation; for ``"auto"`` it stays ``None`` until
    #: ``run_bootstrap_job`` has SSH'd into the nodes (right after
    #: preflight) to discover whatever proxy they already have configured.
    proxy: dict[str, str] | None = None
    #: ``"auto"`` (default) or ``"custom"`` -- see ``dto.BootstrapProxyConfig``.
    proxy_mode: str = "auto"

    def append_log(self, chunk: str) -> None:
        self.log_tail = (self.log_tail + chunk)[-_LOG_TAIL_MAX_CHARS:]


class BootstrapJobStore:
    """Thread-safe in-memory registry of bootstrap jobs."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._jobs: dict[str, BootstrapJob] = {}

    def create(
        self,
        nodes: list[BootstrapNode],
        proxy: dict[str, str] | None = None,
        proxy_mode: str = "auto",
    ) -> BootstrapJob:
        job = BootstrapJob(id=uuid4().hex, nodes=nodes, proxy=proxy, proxy_mode=proxy_mode)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> BootstrapJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def require(self, job_id: str) -> BootstrapJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"bootstrap job {job_id!r} not found")
        return job

    def drop(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)


#: Process-wide singleton, mirroring ``llm_d_bench.cluster.sessions``'s
#: module-level ``_sessions`` dict.
job_store = BootstrapJobStore()
