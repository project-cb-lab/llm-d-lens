"""Request/response DTOs for the bootstrap sub-wizard API (design §5)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from llm_d_bench.cluster.models import StrictModel


class BootstrapNodeInput(StrictModel):
    """One row of the node table. ``host`` accepts a single IP/hostname,
    but also a "host spec" that expands to several hosts sharing this row's
    role selection and SSH credentials -- see
    ``llm_d_bench.cluster.bootstrap.host_expr`` and
    ``docs/design/CLUSTER_BOOTSTRAP_DESIGN.md`` §3.1:

    * a single value: ``10.0.0.5``
    * a comma-separated list: ``10.0.0.5, 10.0.0.6, 10.0.0.9``
    * a shorthand IP range (only the last octet varies): ``10.0.0.10-20``
    * a full IP range: ``10.0.0.250-10.0.1.5``
    """

    host: str = Field(min_length=1, max_length=4096)
    port: int = Field(default=22, ge=1, le=65535)
    #: Non-empty; a node may hold both roles at once (see
    #: docs/design/CLUSTER_BOOTSTRAP_DESIGN.md -- e.g. all-in-one/small clusters where
    #: the control-plane is also schedulable as a worker).
    roles: list[Literal["control-plane", "worker"]] = Field(default_factory=lambda: ["worker"], min_length=1)
    username: str = Field(default="root", min_length=1, max_length=64)
    private_key: str | None = Field(default=None, alias="privateKey")
    password: str | None = None


class BootstrapHostAddress(StrictModel):
    host: str = Field(min_length=1, max_length=4096)
    port: int = Field(default=22, ge=1, le=65535)


class BootstrapHostKeyRequest(StrictModel):
    nodes: list[BootstrapHostAddress] = Field(min_length=1, max_length=64)


class BootstrapHostKeyPin(StrictModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")


class BootstrapHostKeyResult(StrictModel):
    host: str
    port: int
    algorithm: str | None = None
    fingerprint: str | None = None
    error: str | None = None


class BootstrapHostKeyResponse(StrictModel):
    items: list[BootstrapHostKeyResult] = Field(default_factory=list)


class BootstrapProxyConfig(StrictModel):
    """Optional per-job network proxy passthrough for the *target* nodes
    (distinct from the cluster-level "Network Proxy" step later in the
    wizard, which only affects the Prism backend's own llm-d/benchmark
    downloads). Kubespray needs ``http_proxy``/``https_proxy``/``no_proxy``
    exported on each node for OS package installs and the
    ``container-engine/*`` roles' direct-from-GitHub binary downloads
    (e.g. ``runc``) to succeed when a node has no direct internet route.

    * ``mode: "auto"`` (default) -- reuse whatever proxy is already
      configured on each *target* node itself (probed over SSH after
      preflight, before Kubespray runs) rather than the Prism backend
      process's own environment, since the backend and the target nodes
      may sit on entirely different networks.
    * ``mode: "custom"`` -- use the explicit values below instead (any left
      blank are simply omitted from the rendered group_vars).
    """

    mode: Literal["auto", "custom"] = "auto"
    http_proxy: str | None = Field(default=None, alias="httpProxy", max_length=2048)
    https_proxy: str | None = Field(default=None, alias="httpsProxy", max_length=2048)
    no_proxy: str | None = Field(default=None, alias="noProxy", max_length=4096)


class BootstrapPreflightRequest(StrictModel):
    #: Rows, not expanded hosts -- each row's ``host`` may itself expand to
    #: many hosts (see ``BootstrapNodeInput``); the overall expanded host
    #: count is capped separately (``host_expr.MAX_EXPANDED_HOSTS``).
    nodes: list[BootstrapNodeInput] = Field(min_length=1, max_length=64)
    host_keys: list[BootstrapHostKeyPin] = Field(alias="hostKeys", min_length=1)


class BootstrapCheckResult(StrictModel):
    """One named sub-check within a node's overall preflight -- see
    ``preflight.py::PreflightCheckItem``."""

    id: str
    label: str
    status: Literal["passed", "failed", "skipped"]
    detail: str | None = None


class BootstrapNodeResult(StrictModel):
    host: str
    roles: list[Literal["control-plane", "worker"]]
    state: Literal["pending", "running", "passed", "failed"]
    error: str | None = None
    checks: list[BootstrapCheckResult] = Field(default_factory=list)


class BootstrapPreflightResponse(StrictModel):
    items: list[BootstrapNodeResult] = Field(default_factory=list)
    all_passed: bool = Field(alias="allPassed")


class BootstrapCreateRequest(StrictModel):
    """Sent after ``/preflight`` has reported ``allPassed: true`` for every
    node in the same list."""

    nodes: list[BootstrapNodeInput] = Field(min_length=1, max_length=64)
    host_keys: list[BootstrapHostKeyPin] = Field(alias="hostKeys", min_length=1)
    proxy: BootstrapProxyConfig | None = None


class BootstrapCreateResponse(StrictModel):
    bootstrap_id: str = Field(alias="bootstrapId")


class BootstrapStatusResponse(StrictModel):
    id: str
    phase: Literal[
        "queued", "preflight", "provisioning", "running", "kubeconfig_ready", "succeeded", "failed", "cancelled"
    ]
    log_tail: str = Field(default="", alias="logTail")
    error: str | None = None
    # Only populated once, on the response where phase first reaches
    # "succeeded"; the server clears it from memory immediately afterwards
    # (see design §6, "one-time kubeconfig handoff").
    kubeconfig: str | None = None
