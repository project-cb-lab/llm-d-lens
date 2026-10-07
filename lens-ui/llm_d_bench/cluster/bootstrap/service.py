"""Orchestrates a single Kubespray bootstrap job end to end (design §4.2):

    queued -> preflight -> running -> kubeconfig_ready -> succeeded
                                                    \\-> failed / cancelled

Preflight (§3.2) is intentionally exposed as a separate, synchronous-ish
step the frontend calls before ``start_bootstrap`` -- see
``docs/design/CLUSTER_BOOTSTRAP_DESIGN.md`` §2/§3. This module only starts the real,
slow ``ansible-playbook cluster.yml`` run once every node has already
passed that quick check.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import shlex
import shutil
import stat
import tempfile
from pathlib import Path

from llm_d_bench.cluster.bootstrap import kubespray, preflight
from llm_d_bench.cluster.bootstrap.dto import (
    BootstrapHostAddress,
    BootstrapHostKeyPin,
    BootstrapHostKeyResult,
    BootstrapNodeInput,
    BootstrapProxyConfig,
)
from llm_d_bench.cluster.bootstrap.host_expr import MAX_EXPANDED_HOSTS, expand_host_spec
from llm_d_bench.cluster.bootstrap.inventory import artifacts_kubeconfig_path, write_inventory
from llm_d_bench.cluster.bootstrap.models import BootstrapJob, BootstrapNode, job_store
from llm_d_bench.utils.paths import storage_path
from llm_d_bench.utils.shell import CommandNotFoundError, shell, which
from llm_d_bench.utils.ssh import SshTarget, inspect_host_key, remote_exec

logger = logging.getLogger(__name__)

_WORK_ROOT_ENV_DEFAULT = str(storage_path("data", "credentials", "bootstrap"))
_ANSIBLE_TIMEOUT_SECONDS = 3_600.0  # cluster.yml against a handful of nodes; generous ceiling.

#: Live subprocess handles, keyed by job id, kept out of ``BootstrapJob``
#: itself so that dataclass stays a plain, easily-testable value object.
_processes: dict[str, asyncio.subprocess.Process] = {}


def _to_nodes(payload: BootstrapNodeInput) -> list[BootstrapNode]:
    """Expand one input row's ``host`` (a single IP/hostname, a
    comma-separated list, or an IP range -- see ``host_expr.py``) into one
    :class:`BootstrapNode` per literal host, all sharing that row's
    role selection and SSH credentials."""
    return [
        BootstrapNode(
            host=host,
            port=payload.port,
            roles=list(payload.roles),
            username=payload.username,
            private_key=payload.private_key,
            password=payload.password,
        )
        for host in expand_host_spec(payload.host)
    ]


def _expand_addresses(payloads: list[BootstrapHostAddress]) -> list[tuple[str, int]]:
    addresses: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for payload in payloads:
        for host in expand_host_spec(payload.host):
            address = (host, payload.port)
            if address not in seen:
                seen.add(address)
                addresses.append(address)
    if len(addresses) > MAX_EXPANDED_HOSTS:
        raise ValueError(f"host specs expanded to more than the {MAX_EXPANDED_HOSTS}-host limit")
    return addresses


async def discover_host_keys(payloads: list[BootstrapHostAddress]) -> list[BootstrapHostKeyResult]:
    semaphore = asyncio.Semaphore(16)

    async def _inspect(host: str, port: int) -> BootstrapHostKeyResult:
        try:
            async with semaphore:
                key_info = await asyncio.to_thread(inspect_host_key, host, port)
            return BootstrapHostKeyResult(
                host=host, port=port, algorithm=key_info.algorithm, fingerprint=key_info.fingerprint
            )
        except Exception as error:  # surfaced per host so a partial discovery is useful in the UI
            return BootstrapHostKeyResult(host=host, port=port, error=str(error))

    return await asyncio.gather(*(_inspect(host, port) for host, port in _expand_addresses(payloads)))


def _apply_host_key_pins(nodes: list[BootstrapNode], pins: list[BootstrapHostKeyPin]) -> None:
    expected = {(node.host, node.port) for node in nodes}
    supplied: dict[tuple[str, int], str] = {}
    for pin in pins:
        endpoint = (pin.host, pin.port)
        if endpoint in supplied:
            raise ValueError(f"duplicate SSH host-key pin for {pin.host}:{pin.port}")
        supplied[endpoint] = pin.fingerprint
    if supplied.keys() != expected:
        missing = expected - supplied.keys()
        extra = supplied.keys() - expected
        details = []
        if missing:
            details.append("missing: " + ", ".join(f"{host}:{port}" for host, port in sorted(missing)))
        if extra:
            details.append("unexpected: " + ", ".join(f"{host}:{port}" for host, port in sorted(extra)))
        raise ValueError("host-key pins must exactly match expanded bootstrap nodes (" + "; ".join(details) + ")")
    for node in nodes:
        node.host_key_fingerprint = supplied[(node.host, node.port)]


def _write_known_hosts(nodes: list[BootstrapNode], path: Path) -> None:
    entries = []
    for node in nodes:
        key_info = inspect_host_key(node.host, node.port)
        if not node.host_key_fingerprint or not hmac.compare_digest(key_info.fingerprint, node.host_key_fingerprint):
            raise ValueError(f"SSH host key changed for {node.host}:{node.port}; confirm the new fingerprint")
        known_host = node.host if node.port == 22 else f"[{node.host}]:{node.port}"
        entries.append(f"{known_host} {key_info.algorithm} {key_info.key_data}")
    path.write_text("\n".join(entries) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _expand_all(payloads: list[BootstrapNodeInput]) -> list[BootstrapNode]:
    nodes: list[BootstrapNode] = []
    for payload in payloads:
        nodes.extend(_to_nodes(payload))
    if len(nodes) > MAX_EXPANDED_HOSTS:
        raise ValueError(
            f"host specs expanded to {len(nodes)} hosts, which exceeds the {MAX_EXPANDED_HOSTS}-host limit"
        )
    return nodes


def _materialize_key(node: BootstrapNode, keys_dir: Path) -> None:
    if not node.private_key:
        return
    keys_dir.mkdir(parents=True, exist_ok=True)
    key_path = keys_dir / f"{node.name}.key"
    key_path.write_text(node.private_key if node.private_key.endswith("\n") else node.private_key + "\n")
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


def _cleanup_keys(nodes: list[BootstrapNode]) -> None:
    """Drop plaintext key material from memory as soon as it's no longer
    needed by any in-flight step (design §6)."""
    for node in nodes:
        node.private_key = None
        node.password = None


async def run_preflight_check(
    payloads: list[BootstrapNodeInput], pins: list[BootstrapHostKeyPin]
) -> list[BootstrapNode]:
    """One-shot preflight for the "review nodes" screen, decoupled from any
    job: writes any supplied private keys to a throwaway temp dir, runs the
    checks, then removes the temp dir immediately regardless of outcome."""
    nodes = _expand_all(payloads)
    _apply_host_key_pins(nodes, pins)
    with tempfile.TemporaryDirectory(prefix="llm-d-bench-bootstrap-preflight-") as tmp:
        keys_dir = Path(tmp) / "keys"
        for node in nodes:
            _materialize_key(node, keys_dir)
            if node.private_key:
                node.private_key_path = str(keys_dir / f"{node.name}.key")
        try:
            await preflight.run_preflight(nodes)
        finally:
            _cleanup_keys(nodes)
    return nodes


def _work_root() -> Path:
    return Path(_WORK_ROOT_ENV_DEFAULT).expanduser().resolve()


def _resolve_custom_proxy(config: BootstrapProxyConfig) -> dict[str, str] | None:
    """Turn an explicit ``mode="custom"`` ``BootstrapProxyConfig`` into the
    plain ``{"http_proxy": ..., ...}`` dict ``render_group_vars`` expects."""
    resolved = {
        "http_proxy": config.http_proxy,
        "https_proxy": config.https_proxy,
        "no_proxy": config.no_proxy,
    }
    resolved = {key: value for key, value in resolved.items() if value}
    return resolved or None


_PROXY_PROBE_COMMAND = 'cat /etc/environment 2>/dev/null; env | grep -i "_proxy=" || true'


def _parse_proxy_probe_output(output: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().strip('"').strip("'").lower()
        value = value.strip().strip('"').strip("'")
        if key in ("http_proxy", "https_proxy", "no_proxy") and value and key not in found:
            found[key] = value
    return found


def _node_ssh_target(node: BootstrapNode) -> SshTarget:
    return SshTarget(
        host=node.host,
        port=node.port,
        username=node.username,
        key_filename=node.private_key_path,
        password=node.password,
        host_key_fingerprint=node.host_key_fingerprint,
    )


def _probe_node_proxy(node: BootstrapNode) -> dict[str, str]:
    """Best-effort: SSH into ``node`` (reusing the same credentials
    preflight just validated) and look for a proxy that's *already*
    configured on that node itself -- ``/etc/environment`` plus any
    ``*_proxy``/``*_PROXY`` variable already exported in the login
    environment. Any failure here is swallowed (logged) rather than
    failing the whole job; worst case "auto" mode just finds no proxy."""
    try:
        result = remote_exec(_node_ssh_target(node), _PROXY_PROBE_COMMAND, timeout=15)
    except Exception as error:  # network/auth hiccups shouldn't fail the whole job
        logger.warning("could not probe %s for an existing proxy configuration: %s", node.host, error)
        return {}
    if result.returncode != 0:
        return {}
    return _parse_proxy_probe_output(result.stdout)


async def _resolve_auto_proxy_from_nodes(nodes: list[BootstrapNode]) -> dict[str, str] | None:
    """ "Auto" mode (design §3.1): reuse whatever proxy is *already
    configured on the target nodes being deployed* -- not the Prism
    backend host's own environment, since the backend and the target
    nodes may sit on entirely different networks. Probes each node over
    SSH (off the event loop), taking the first non-empty
    ``http_proxy``/``https_proxy`` found and unioning every node's
    ``no_proxy`` entries."""
    merged: dict[str, str] = {}
    no_proxy_chunks: list[str] = []
    for node in nodes:
        found = await asyncio.to_thread(_probe_node_proxy, node)
        for key in ("http_proxy", "https_proxy"):
            if found.get(key) and key not in merged:
                merged[key] = found[key]
        if found.get("no_proxy"):
            no_proxy_chunks.append(found["no_proxy"])
    if no_proxy_chunks:
        seen: list[str] = []
        for chunk in no_proxy_chunks:
            for item in chunk.split(","):
                item = item.strip()
                if item and item not in seen:
                    seen.append(item)
        merged["no_proxy"] = ",".join(seen)
    return merged or None


def create_job(
    payloads: list[BootstrapNodeInput],
    pins: list[BootstrapHostKeyPin],
    proxy: BootstrapProxyConfig | None = None,
) -> BootstrapJob:
    nodes = _expand_all(payloads)
    _apply_host_key_pins(nodes, pins)
    mode = proxy.mode if proxy is not None else "auto"
    # "custom" resolves immediately (no SSH needed); "auto" is resolved
    # later in run_bootstrap_job, once preflight has confirmed each node
    # is reachable, by probing the nodes themselves (see
    # ``_resolve_auto_proxy_from_nodes``).
    resolved = _resolve_custom_proxy(proxy) if (proxy is not None and mode == "custom") else None
    return job_store.create(nodes, proxy=resolved, proxy_mode=mode)


async def _stream_output(process: asyncio.subprocess.Process, job: BootstrapJob) -> None:
    assert process.stdout is not None
    async for raw_line in process.stdout:
        job.append_log(raw_line.decode(errors="replace"))


async def run_bootstrap_job(job_id: str) -> None:
    """Runs to completion (or failure/cancellation); intended to be
    scheduled with ``asyncio.create_task`` right after ``create_job``."""
    job = job_store.require(job_id)
    work_dir = _work_root() / job.id
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    job.work_dir = work_dir
    keys_dir = work_dir / "keys"
    known_hosts_path = work_dir / "known_hosts"

    try:
        job.phase = "preflight"
        for node in job.nodes:
            _materialize_key(node, keys_dir)
            if node.private_key:
                node.private_key_path = str(keys_dir / f"{node.name}.key")
        await preflight.run_preflight(job.nodes)
        failed = [node for node in job.nodes if node.preflight_state != "passed"]
        if failed:
            job.phase = "failed"
            job.error = "; ".join(f"{node.host}: {node.preflight_error}" for node in failed)
            return

        uses_password_auth = any(not node.private_key_path and node.password for node in job.nodes)
        if uses_password_auth and which("sshpass") is None:
            job.phase = "failed"
            job.error = (
                "one or more nodes use password authentication, but 'sshpass' is not installed on "
                "the Prism backend host (required by Ansible's ssh connection plugin to supply "
                "ansible_ssh_pass) -- install it (e.g. `apt-get install sshpass`) or use an SSH "
                "private key for those nodes instead"
            )
            return

        if job.proxy_mode == "auto":
            job.proxy = await _resolve_auto_proxy_from_nodes(job.nodes)
        write_inventory(work_dir, job.nodes, proxy=job.proxy)
        await asyncio.to_thread(_write_known_hosts, job.nodes, known_hosts_path)

        try:
            checkout = kubespray.require_ready()
        except CommandNotFoundError:
            # Not provisioned yet -- auto-provision it now rather than
            # failing the job outright (design decision: an operator no
            # longer needs to pre-run ``ensure_ready()`` out-of-band; the
            # first bootstrap job just takes longer while Kubespray is
            # cloned + its dedicated venv is built). This is a slow,
            # blocking operation (git clone + pip install), so it must run
            # off the event loop.
            job.phase = "provisioning"
            try:
                checkout = await asyncio.to_thread(kubespray.ensure_ready)
            except Exception as error:  # git/pip failures, no network, etc.
                job.phase = "failed"
                job.error = f"failed to auto-provision Kubespray: {error}"
                return

        job.phase = "running"
        argv = [
            str(kubespray.ansible_playbook_path()),
            "-i",
            str(work_dir / "inventory.yml"),
            "-b",
            str(kubespray.cluster_playbook_path()),
        ]
        process = await shell.spawn(
            argv,
            cwd=str(checkout),
            env={
                "ANSIBLE_HOST_KEY_CHECKING": "True",
                "ANSIBLE_SSH_COMMON_ARGS": (
                    f"-o UserKnownHostsFile={shlex.quote(str(known_hosts_path))} "
                    "-o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null"
                ),
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        _processes[job.id] = process
        job.process_pid = process.pid
        try:
            await asyncio.wait_for(
                asyncio.gather(_stream_output(process, job), process.wait()),
                timeout=_ANSIBLE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            job.phase = "failed"
            job.error = f"ansible-playbook timed out after {_ANSIBLE_TIMEOUT_SECONDS:.0f}s"
            return
        finally:
            _processes.pop(job.id, None)

        if job.cancelled:
            job.phase = "cancelled"
            return
        if process.returncode != 0:
            job.phase = "failed"
            job.error = f"ansible-playbook exited with status {process.returncode}"
            return

        job.phase = "kubeconfig_ready"
        kubeconfig_path = artifacts_kubeconfig_path(work_dir)
        if not kubeconfig_path.is_file():
            job.phase = "failed"
            job.error = "ansible-playbook succeeded but no admin.conf artifact was produced"
            return
        job.kubeconfig_text = kubeconfig_path.read_text()
        job.phase = "succeeded"
    except Exception as error:  # noqa: BLE001 - surfaced to the user via job.error
        logger.exception("event=bootstrap_job_failed job_id=%s", job.id)
        job.phase = "failed"
        job.error = str(error)
    finally:
        _cleanup_keys(job.nodes)
        # Belt-and-suspenders on top of the in-memory wipe above: the
        # private-key/password material also briefly touched disk (as
        # ``keys/*.key`` / ``host_vars/*.yml``) for ansible-playbook to
        # read -- remove those specific files regardless of how the job
        # ended, rather than waiting for the whole ``work_dir`` to be
        # cleaned up later (``consume_kubeconfig``/cancel, which may never
        # be called if the job failed). ``artifacts/`` (the kubeconfig) and
        # ``inventory.yml`` remain in the private credentials directory;
        # ``consume_kubeconfig``/cancellation still need the directory.
        shutil.rmtree(keys_dir, ignore_errors=True)
        shutil.rmtree(work_dir / "host_vars", ignore_errors=True)
        known_hosts_path.unlink(missing_ok=True)


def get_job(job_id: str) -> BootstrapJob:
    return job_store.require(job_id)


def consume_kubeconfig(job_id: str) -> str:
    """Return the kubeconfig text exactly once, then wipe it from memory and
    disk (design §6, "one-time kubeconfig handoff")."""
    job = job_store.require(job_id)
    if job.phase != "succeeded" or job.kubeconfig_text is None:
        raise ValueError("bootstrap job has not produced a kubeconfig yet")
    kubeconfig_text = job.kubeconfig_text
    job.kubeconfig_text = None
    _cleanup_job_dir(job)
    return kubeconfig_text


def cancel_job(job_id: str) -> None:
    job = job_store.require(job_id)
    job.cancelled = True
    process = _processes.get(job_id)
    if process is not None and process.returncode is None:
        process.terminate()


def _cleanup_job_dir(job: BootstrapJob) -> None:
    if job.work_dir is not None:
        shutil.rmtree(job.work_dir, ignore_errors=True)
    job_store.drop(job.id)
