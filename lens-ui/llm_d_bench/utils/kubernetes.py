"""Kubernetes endpoint discovery used by Simulation's in-cluster mode."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query

from llm_d_bench.utils.shell import CommandResult, CommandRunner, ScopedCommandRunner, spawn, which

router = APIRouter(prefix="/api/cluster", tags=["cluster"])

logger = logging.getLogger(__name__)


_NAMESPACE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


def validate_namespace(value: str) -> str:
    """Validate a Kubernetes namespace without normalizing caller input."""
    if len(value) > 63 or not _NAMESPACE.fullmatch(value):
        raise ValueError("namespace must be a valid Kubernetes DNS label")
    return value


@dataclass
class PortForward:
    id: str
    namespace: str
    service: str
    remote_port: int
    address: str | None
    process: asyncio.subprocess.Process
    local_port: int
    session_id: str | None = None
    cluster_id: str | None = None

    @property
    def key(self) -> tuple[str, str, int, str | None, str | None]:
        return (self.namespace, self.service, self.remote_port, self.address, self.cluster_id)


class PortForwardError(Exception):
    """Raised when a port-forward cannot be established."""


_port_forwards: dict[tuple[str, str, int, str | None, str | None], PortForward] = {}
_PORT_RANGE = range(18000, 19000)
_PORT_FORWARD_MAX_RESTARTS = 5
_RESOURCE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# How long to wait for `kubectl port-forward` to report it is actively
# forwarding (and for the local port to actually accept a connection) before
# giving up on that attempt. Deliberately generous: this only gates
# background tunnel setup/reconcile, never a live client request, and slow
# clusters/cold pods can take a few seconds to accept the forwarded stream.
_PORT_FORWARD_READY_TIMEOUT = 15.0
_PORT_FORWARD_READY_MARKER = "Forwarding from"


def _validate_resource_name(value: str, field: str) -> str:
    if len(value) > 253 or not _RESOURCE_NAME.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"{field} must be a valid Kubernetes name")
    return value


def kubeconfig_environment(cluster_id: str | None = None, *, strict: bool = False) -> dict[str, str]:
    """Environment pointing ``kubectl`` at a cluster's kubeconfig.

    With ``cluster_id`` set, resolves that cluster's kubeconfig and injects
    ``KUBECONFIG``. Otherwise no ``KUBECONFIG`` is injected, so ``kubectl`` uses
    its ambient context. ``strict`` refuses an unresolved explicit cluster rather
    than inheriting the environment; SDK reads enable it. Legacy CLI callers keep
    their existing resolution behavior.
    """
    environment = dict(os.environ)
    if cluster_id:
        from llm_d_bench.cluster import registry, sessions

        cluster = registry.get_cluster(cluster_id)
        if cluster and registry.kubeconfig_path(cluster.id).is_file():
            environment["KUBECONFIG"] = str(registry.kubeconfig_path(cluster.id))
        elif sessions.session_kubeconfig_path(cluster_id).is_file():
            environment["KUBECONFIG"] = str(sessions.session_kubeconfig_path(cluster_id))
        else:
            session = sessions.get_session_for_server(cluster_id)
            if session and session.kubeconfig_path.is_file():
                environment["KUBECONFIG"] = str(session.kubeconfig_path)
            elif strict:
                raise FileNotFoundError(f"cluster {cluster_id!r} has no available kubeconfig")
            elif cluster is None:
                raise FileNotFoundError(f"cluster {cluster_id!r} not found")
    return environment


async def run_kubectl(
    command: list[str],
    timeout: float = 10,
    cluster_id: str | None = None,
) -> CommandResult:
    """Run ``kubectl`` against a cluster's kubeconfig (or the ambient context).

    With ``cluster_id`` None the ambient context is used; otherwise the cluster's
    kubeconfig is injected via ``KUBECONFIG``. Raises :class:`FileNotFoundError`
    when ``kubectl`` is missing or the cluster is unknown, and
    :class:`TimeoutError` when the command exceeds ``timeout``.
    """
    return await scoped_runner(cluster_id).run(["kubectl", *command], timeout=timeout)


def scoped_runner(cluster_id: str | None) -> CommandRunner:
    """Return a command runner pointed at a cluster's kubeconfig.

    With ``cluster_id`` None the ambient context is used; otherwise the cluster's
    kubeconfig is injected via ``KUBECONFIG``. Raises :class:`FileNotFoundError`
    when ``cluster_id`` references an unknown cluster.
    """
    from llm_d_bench.utils.kubernetes_commands import sdk_enabled

    if sdk_enabled():
        return KubernetesCommandRunner(kubeconfig_environment(cluster_id, strict=True))
    if cluster_id is None:
        return CommandRunner()
    return ScopedCommandRunner(kubeconfig_environment(cluster_id))


class KubernetesCommandRunner(ScopedCommandRunner):
    """Known resource operations use SDK; other tools retain the shared runner."""

    async def run(
        self,
        argv,
        *,
        cwd=None,
        env=None,
        input=None,  # noqa: A002 - preserves CommandRunner's input= keyword
        timeout=10,
    ):
        from llm_d_bench.utils.kubernetes_commands import execute_sdk_command, sdk_enabled, sdk_writes_enabled

        if sdk_enabled() and cwd is None:
            environment = self._merged_env(env)
            result = await execute_sdk_command(
                argv,
                kubeconfig=environment.get("KUBECONFIG"),
                timeout=timeout,
                stdin_data=input,
                allow_writes=sdk_writes_enabled(),
            )
            if result is not None:
                if result.returncode == 124:
                    from llm_d_bench.utils.shell import CommandTimeoutError

                    raise CommandTimeoutError(result.stderr)
                return result
        return await super().run(argv, cwd=cwd, env=env, input=input, timeout=timeout)


@router.on_event("startup")
async def start_kubernetes_clients():
    from llm_d_bench.utils.kubernetes_pool import start_pool

    await start_pool()


@router.on_event("shutdown")
async def close_kubernetes_clients():
    from llm_d_bench.utils.kubernetes_pool import close_pool

    await close_pool()


async def list_resources(
    resource: str,
    *,
    namespace: str | None = None,
    selector: str | None = None,
    all_namespaces: bool = False,
    cluster_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return raw Kubernetes items through the configured resource-read backend.

    Failures (including clusters that do not expose the resource) yield an empty
    list, so callers can treat "not found" uniformly. SDK is the default;
    PRISM_KUBERNETES_BACKEND=cli explicitly restores the CLI backend.
    Configuration errors propagate. SDK request failures never retry via CLI.
    """
    from llm_d_bench.utils.kubernetes_commands import sdk_enabled

    if sdk_enabled():
        from llm_d_bench.utils.kubernetes_reads import (
            CliAuthenticationRequiredError,
            list_sdk_resources,
            supports_resource,
        )

        if supports_resource(resource):
            environment = kubeconfig_environment(cluster_id, strict=True)
            try:
                return await list_sdk_resources(
                    resource,
                    kubeconfig=environment.get("KUBECONFIG"),
                    namespace=namespace,
                    selector=selector,
                    all_namespaces=all_namespaces,
                )
            except CliAuthenticationRequiredError:
                logger.info("Kubernetes resource read uses CLI for dynamic authentication")
    argv = ["kubectl", "get", resource]
    if all_namespaces:
        argv.append("--all-namespaces")
    elif namespace:
        argv += ["-n", namespace]
    if selector:
        argv += ["-l", selector]
    argv += ["-o", "json"]
    result = await scoped_runner(cluster_id).run(argv)
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout or "{}").get("items") or []
    except json.JSONDecodeError:
        return []


async def current_context(cluster_id: str | None = None) -> str | None:
    """Return the cluster's current kubectl context name, or None."""
    result = await scoped_runner(cluster_id).run(["kubectl", "config", "current-context"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


async def cluster_info(cluster_id: str | None = None) -> CommandResult:
    """Return the result of ``kubectl cluster-info`` (used to probe reachability)."""
    return await scoped_runner(cluster_id).run(["kubectl", "cluster-info"])


async def get_namespace(namespace: str, *, cluster_id: str | None = None) -> dict[str, Any] | None:
    """Return a parsed namespace object, or None when it does not exist."""
    result = await scoped_runner(cluster_id).run(["kubectl", "get", "namespace", namespace, "-o", "json"])
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None


async def label_node(
    name: str,
    labels: Mapping[str, str],
    *,
    cluster_id: str | None = None,
    overwrite: bool = True,
) -> CommandResult:
    """Apply (or overwrite) labels on a Kubernetes node."""
    argv = ["kubectl", "label", "node", name]
    argv += [f"{key}={value}" for key, value in labels.items()]
    if overwrite:
        argv.append("--overwrite")
    return await scoped_runner(cluster_id).run(argv)


async def helm_status(release: str, namespace: str, *, cluster_id: str | None = None) -> CommandResult:
    """Return the result of ``helm status <release> -o json``."""
    return await scoped_runner(cluster_id).run(["helm", "status", release, "-n", namespace, "-o", "json"])


async def helm_list(
    namespace: str, *, filter_regex: str | None = None, cluster_id: str | None = None
) -> list[dict[str, Any]]:
    """Return parsed ``helm list -o json`` rows, or [] on failure."""
    argv = ["helm", "list", "-n", namespace, "-o", "json"]
    if filter_regex:
        argv += ["--filter", filter_regex]
    result = await scoped_runner(cluster_id).run(argv)
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []


async def helm_get_values(release: str, namespace: str, *, cluster_id: str | None = None) -> dict[str, Any]:
    """Return parsed ``helm get values -o json``, or {} on failure."""
    result = await scoped_runner(cluster_id).run(["helm", "get", "values", release, "-n", namespace, "-o", "json"])
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}


async def namespace_exists(namespace: str, *, cluster_id: str | None = None) -> bool:
    """Return whether a Kubernetes namespace exists."""
    result = await scoped_runner(cluster_id).run(["kubectl", "get", "namespace", namespace, "-o", "name"])
    return result.returncode == 0


async def create_namespace(namespace: str, *, cluster_id: str | None = None) -> CommandResult:
    """Create a Kubernetes namespace."""
    return await scoped_runner(cluster_id).run(["kubectl", "create", "namespace", namespace])


async def delete_namespace(
    namespace: str, *, cluster_id: str | None = None, timeout: float | None = 60
) -> CommandResult:
    """Delete a Kubernetes namespace (and everything in it). Best-effort caller
    should not treat a missing namespace as fatal -- pass ``--ignore-not-found``
    so a namespace that was never created (or already gone) is a no-op."""
    return await scoped_runner(cluster_id).run(
        ["kubectl", "delete", "namespace", namespace, "--ignore-not-found"], timeout=timeout
    )


async def label_namespace(
    namespace: str,
    labels: Mapping[str, str],
    *,
    cluster_id: str | None = None,
    overwrite: bool = True,
) -> CommandResult:
    """Apply (or overwrite) labels on a Kubernetes namespace."""
    argv = ["kubectl", "label", "namespace", namespace]
    argv += [f"{key}={value}" for key, value in labels.items()]
    if overwrite:
        argv.append("--overwrite")
    return await scoped_runner(cluster_id).run(argv)


def _service_endpoint(namespace: str, item: dict[str, Any]) -> dict[str, Any] | None:
    metadata = item.get("metadata") or {}
    spec = item.get("spec") or {}
    service = str(metadata.get("name") or "")
    if not service or not any(token in service.lower() for token in ("gateway", "istio", "inference")):
        return None
    ports = spec.get("ports") or []
    preferred = next(
        (
            port
            for port in ports
            if str(port.get("port")) == "80" or str(port.get("name") or "").lower() in {"http", "default", "web"}
        ),
        None,
    )
    if preferred is None:
        preferred = next(
            (port for port in ports if str(port.get("name") or "").lower() not in {"status", "status-port", "metrics"}),
            {},
        )
    port = int(preferred.get("port", 80))
    return {
        "namespace": namespace,
        "service": service,
        "type": spec.get("type", "ClusterIP"),
        "cluster_ip": spec.get("clusterIP"),
        "port": port,
        "internal_url": f"http://{service}.{namespace}.svc.cluster.local:{port}",
        "local_url": None,
        "model": None,
    }


@router.get("/endpoints")
async def discover_endpoints(
    namespace: str | None = Query(default=None, max_length=253),
    cluster_id: str | None = Query(default=None, max_length=64),
) -> dict:
    try:
        if namespace:
            namespaces = [_validate_resource_name(namespace, "namespace")]
        else:
            result = await run_kubectl(
                ["get", "namespaces", "-o", "json"],
                cluster_id=cluster_id,
            )
            if result.returncode != 0:
                return {"endpoints": [], "total": 0, "error": result.stderr.strip()}
            namespace_items = json.loads(result.stdout).get("items", [])
            all_namespaces = [str((item.get("metadata") or {}).get("name") or "") for item in namespace_items]
            relevant = [
                name
                for name in all_namespaces
                if name and any(token in name.lower() for token in ("llm", "inference", "model", "bench"))
            ]
            namespaces = (relevant or all_namespaces)[:50]

        endpoints: list[dict[str, Any]] = []
        for current_namespace in namespaces:
            result = await run_kubectl(
                ["get", "services", "-n", current_namespace, "-o", "json"],
                cluster_id=cluster_id,
            )
            if result.returncode != 0:
                continue
            for item in json.loads(result.stdout).get("items", []):
                endpoint = _service_endpoint(current_namespace, item)
                if endpoint is not None:
                    endpoints.append(endpoint)
        return {"endpoints": endpoints, "total": len(endpoints)}
    except FileNotFoundError as error:
        return {"endpoints": [], "total": 0, "error": str(error)}
    except TimeoutError:
        return {"endpoints": [], "total": 0, "error": "Timed out while querying Kubernetes"}
    except (json.JSONDecodeError, OSError) as error:
        raise HTTPException(status_code=502, detail=f"Unable to query Kubernetes: {error}") from error


async def _launch_port_forward(
    namespace: str,
    service: str,
    local_port: int,
    remote_port: int,
    *,
    address: str | None = None,
    env: Mapping[str, str] | None = None,
    cluster_id: str | None = None,
) -> asyncio.subprocess.Process:
    """Spawn a ``kubectl port-forward`` process without waiting for it."""
    command = ["kubectl", "port-forward"]
    if address:
        command += ["--address", address]
    command += ["-n", namespace, f"service/{service}", f"{local_port}:{remote_port}"]
    if env is None:
        env = kubeconfig_environment(cluster_id)
    return await spawn(
        command,
        # stdout is piped (not DEVNULL) so `_wait_port_forward` can watch for
        # kubectl's own "Forwarding from" readiness line below.
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


async def _drain_stream(stream: asyncio.StreamReader | None) -> None:
    """Consume a subprocess stream so its pipe never fills and blocks it."""
    if stream is None:
        return
    try:
        while await stream.readline():
            pass
    except (asyncio.CancelledError, ValueError):
        pass


async def _probe_tcp(host: str, port: int, *, timeout: float) -> bool:
    """Return ``True`` if a bare TCP connection to ``host:port`` succeeds."""
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def _read_until_forwarding(process: asyncio.subprocess.Process, deadline: float) -> tuple[bool, str]:
    """Read kubectl's stdout until it reports the tunnel is forwarding.

    Returns ``(True, "")`` once kubectl prints its "Forwarding from" line, or
    ``(False, reason)`` if the process exits, stdout closes, or ``deadline``
    (an ``asyncio`` loop-clock timestamp) passes first.
    """
    stdout = process.stdout
    if stdout is None:
        return False, "port-forward process has no stdout"
    loop = asyncio.get_running_loop()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False, "timed out waiting for port-forward to report ready"
        try:
            line = await asyncio.wait_for(stdout.readline(), timeout=remaining)
        except TimeoutError:
            return False, "timed out waiting for port-forward to report ready"
        if not line:
            return False, "port-forward stdout closed before reporting ready"
        if _PORT_FORWARD_READY_MARKER in line.decode(errors="replace"):
            return True, ""


async def _wait_port_forward(
    process: asyncio.subprocess.Process,
    *,
    local_port: int,
    address: str | None = None,
    timeout: float = _PORT_FORWARD_READY_TIMEOUT,
) -> tuple[bool, str]:
    """Confirm a ``kubectl port-forward`` tunnel is actually usable.

    A tunnel is only declared ready once (1) kubectl has printed its own
    "Forwarding from" line, confirming it finished negotiating the stream
    with the API server/kubelet, *and* (2) a real TCP connection to the
    local port succeeds. Merely checking that the process hasn't exited yet
    (the previous behavior) can report "ready" before the local listener is
    actually accepting connections; the first request routed through it then
    fails with a "Connection refused" upstream error while a near-instant
    retry succeeds. Waiting for positive evidence here removes that race
    instead of pushing retries onto callers.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    reported, reason = await _read_until_forwarding(process, deadline)
    if not reported:
        if process.returncode is None:
            await _terminate_process(process)
        stderr = b""
        if process.stderr is not None:
            try:
                stderr = await asyncio.wait_for(process.stderr.read(), timeout=2)
            except TimeoutError:
                stderr = b""
        return False, stderr.decode(errors="replace").strip() or reason

    probe_host = "127.0.0.1" if address in (None, "0.0.0.0") else address  # noqa: S104 - comparison, not a bind
    while loop.time() < deadline:
        if await _probe_tcp(probe_host, local_port, timeout=max(0.5, deadline - loop.time())):
            return True, ""
        if process.returncode is not None:
            break
        await asyncio.sleep(0.1)

    if process.returncode is None:
        await _terminate_process(process)
    stderr = b""
    if process.stderr is not None:
        try:
            stderr = await asyncio.wait_for(process.stderr.read(), timeout=2)
        except TimeoutError:
            stderr = b""
    return False, stderr.decode(errors="replace").strip() or (
        "port-forward reported ready but never accepted a connection"
    )


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a running process, escalating to ``kill`` after a timeout."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


async def ensure_port_forward(
    namespace: str,
    service: str,
    remote_port: int,
    *,
    local_port: int = 0,
    address: str | None = None,
    env: Mapping[str, str] | None = None,
    cluster_id: str | None = None,
    session_id: str | None = None,
) -> PortForward:
    """Start (or reuse) a ``kubectl port-forward`` tunnel and return its handle.

    Tunnels are cached by ``(namespace, service, remote_port, address, cluster_id)`` so
    repeated requests for the same service reuse the same process. Pass
    ``address="0.0.0.0"`` to make the tunnel reachable from other machines (e.g.
    browser dashboards); the default (``None``) binds to localhost. When ``cluster_id``
    is provided its kubeconfig is used; otherwise the current cluster is used.
    """
    key = (namespace, service, remote_port, address, cluster_id)
    existing = _port_forwards.get(key)
    if existing is not None and existing.process.returncode is None:
        return existing

    if env is None:
        env = kubeconfig_environment(cluster_id)

    if which("kubectl") is None:
        raise PortForwardError("kubectl is not installed")

    last_error = "kubectl port-forward failed"
    for _ in range(3):
        selected_port = local_port or random.SystemRandom().choice(_PORT_RANGE)
        process = await _launch_port_forward(namespace, service, selected_port, remote_port, address=address, env=env)
        ready, stderr = await _wait_port_forward(process, local_port=selected_port, address=address)
        if ready:
            forward = PortForward(
                id=str(uuid4()),
                namespace=namespace,
                service=service,
                remote_port=remote_port,
                address=address,
                process=process,
                local_port=selected_port,
                session_id=session_id,
                cluster_id=cluster_id,
            )
            _port_forwards[key] = forward
            asyncio.create_task(_watch_port_forward(forward))
            if session_id:
                asyncio.create_task(_watch_port_forward_session(forward))
            return forward
        if stderr:
            last_error = stderr
    raise PortForwardError(last_error)


@router.post("/port-forward")
async def start_port_forward(
    namespace: str = Query(max_length=253),
    service: str = Query(max_length=253),
    local_port: int = Query(default=0, ge=0, le=65535),
    remote_port: int = Query(default=80, ge=1, le=65535),
    cluster_id: str | None = Query(default=None, max_length=64),
) -> dict:
    namespace = _validate_resource_name(namespace, "namespace")
    service = _validate_resource_name(service, "service")
    if which("kubectl") is None:
        raise HTTPException(status_code=503, detail="kubectl is not installed")
    try:
        forward = await ensure_port_forward(
            namespace, service, remote_port, local_port=local_port, cluster_id=cluster_id
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PortForwardError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return {
        "success": True,
        "id": forward.id,
        "local_port": forward.local_port,
        "endpoint": f"http://127.0.0.1:{forward.local_port}",
    }


async def _watch_port_forward(forward: PortForward) -> None:
    """Keep a ``kubectl port-forward`` alive, restarting it on the same local port.

    ``kubectl port-forward`` occasionally drops its tunnel (for example with
    "lost connection to pod"), which leaves in-flight simulation requests
    failing with connection errors and every later request refused. This
    watcher transparently relaunches the tunnel on the *same* local port so the
    endpoint URL stays stable and subsequent requests recover automatically.
    """
    restarts = 0
    while _port_forwards.get(forward.key) is forward:
        process = forward.process
        drain_tasks = [
            asyncio.create_task(_drain_stream(process.stdout)),
            asyncio.create_task(_drain_stream(process.stderr)),
        ]
        await process.wait()
        for task in drain_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if _port_forwards.get(forward.key) is not forward:
            return
        if restarts >= _PORT_FORWARD_MAX_RESTARTS:
            logger.warning(
                "port-forward %s:%d dropped %d times; giving up",
                forward.service,
                forward.local_port,
                restarts,
            )
            _port_forwards.pop(forward.key, None)
            return
        restarts += 1
        delay = min(2.0**restarts, 30.0)
        logger.warning(
            "port-forward %s:%d exited (restart %d/%d); relaunching in %.1fs",
            forward.service,
            forward.local_port,
            restarts,
            _PORT_FORWARD_MAX_RESTARTS,
            delay,
        )
        await asyncio.sleep(delay)
        if _port_forwards.get(forward.key) is not forward:
            return
        try:
            relaunched = await _launch_port_forward(
                forward.namespace,
                forward.service,
                forward.local_port,
                forward.remote_port,
                address=forward.address,
                env=kubeconfig_environment(forward.cluster_id),
            )
        except (FileNotFoundError, OSError):
            _port_forwards.pop(forward.key, None)
            return
        ready, _stderr = await _wait_port_forward(relaunched, local_port=forward.local_port, address=forward.address)
        if not ready:
            logger.warning(
                "port-forward %s:%d relaunch did not become ready; will retry",
                forward.service,
                forward.local_port,
            )
        forward.process = relaunched


async def _watch_port_forward_session(forward: PortForward) -> None:
    from llm_d_bench.cluster import require_active_session

    while _port_forwards.get(forward.key) is forward and forward.process.returncode is None:
        await asyncio.sleep(5)
        try:
            require_active_session(forward.session_id)
        except ValueError:
            await _stop_port_forward(forward)
            return


async def _stop_port_forward(forward: PortForward) -> None:
    _port_forwards.pop(forward.key, None)
    await _terminate_process(forward.process)


@router.delete("/port-forwards/{forward_id}", status_code=204)
async def stop_port_forward(forward_id: str) -> None:
    forward = next((item for item in _port_forwards.values() if item.id == forward_id), None)
    if forward is None:
        raise HTTPException(status_code=404, detail="port-forward not found")
    await _stop_port_forward(forward)


async def stop_port_forward_for(
    namespace: str,
    service: str,
    remote_port: int,
    *,
    address: str | None = None,
    cluster_id: str | None = None,
) -> None:
    """Stop the cached tunnel for one ``(namespace, service, port, address, cluster)``."""
    forward = _port_forwards.get((namespace, service, remote_port, address, cluster_id))
    if forward is not None:
        await _stop_port_forward(forward)


@router.delete("/sessions/{session_id}/port-forwards", status_code=204)
async def stop_session_port_forwards(session_id: str) -> None:
    from llm_d_bench.cluster import require_active_session

    with contextlib.suppress(ValueError):
        require_active_session(session_id)
    forwards = [forward for forward in _port_forwards.values() if forward.session_id == session_id]
    await asyncio.gather(*(_stop_port_forward(forward) for forward in forwards))


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    from llm_d_bench.cluster import require_active_session

    try:
        session = require_active_session(session_id)
        return {
            "id": session.id,
            "server_id": session.server_id,
            "created_at": session.created_at,
            "status": "connected",
        }
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def shutdown_port_forwards() -> None:
    await asyncio.gather(*(_stop_port_forward(forward) for forward in list(_port_forwards.values())))


router.add_event_handler("shutdown", shutdown_port_forwards)
