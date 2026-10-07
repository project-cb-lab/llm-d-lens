"""Run Simulation backend commands from inside the deployment's cluster.

``kubectl port-forward service/X`` pins its whole tunnel to a single backing
pod for the tunnel's lifetime (kube-proxy only load-balances real Service
traffic, not a forwarded stream), so testing a "deployment" endpoint through a
host-side port-forward drives every generated request at one replica. This
module instead launches a short-lived Pod inside the deployment's namespace
and runs the backend executable from there via ``kubectl exec``, reaching the
Service's in-cluster DNS name like any other in-cluster client so requests are
load-balanced across every replica the same way real traffic is.

The pod is seeded (via ``kubectl cp``) with the same task directory and trace
data root the host uses, at identical absolute paths, so backend commands
built by :mod:`llm_d_bench.simulation.backends` need no changes: every
``--artifact-dir``/input-file argument they already produce resolves to the
same path inside the pod as on the host. Once the command finishes, the
artifact directory is copied back so :meth:`CommandBackend.parse` and the
rest of the analytics pipeline keep reading from local disk, unmodified.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from llm_d_bench.utils.kubernetes import kubeconfig_environment, scoped_runner
from llm_d_bench.utils.paths import storage_path
from llm_d_bench.utils.shell import spawn

from .errors import SimulationCancelledError, SimulationConfigurationError, SimulationExecutionError
from .models import SimulationTask
from .progress import report_request_progress
from .process import CommandResult, RunContext, _copy_stream, terminate_process, utc_now
from .traces import BaseTrace

_IMAGE_ENV_VAR = "SIMULATION_INCLUSTER_IMAGE"
_DEFAULT_INCLUSTER_IMAGE = "python:3.12-slim"
_AIPERF_IMAGE_ENV_VAR = "SIMULATION_AIPERF_INCLUSTER_IMAGE"
# AIPerf publishes a ready-to-run image with the exact pinned CLI version
# already installed (at ``/opt/aiperf/venv/bin/aiperf``, already on ``PATH``),
# so in-cluster aiperf runs use it directly instead of ``pip install``-ing
# aiperf on top of a bare Python image on every pod launch.
_DEFAULT_AIPERF_INCLUSTER_IMAGE = "nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0"
_POD_LABEL = "llm-d-lens-simulation-runner"
_POD_STARTUP_TIMEOUT_SECONDS = 120
_POD_STARTUP_POLL_SECONDS = 2


def incluster_image(tool_name: str | None = None) -> str:
    """Return the container image used for in-cluster simulation pods.

    ``tool_name`` selects a tool-appropriate default: aiperf runs use the
    official ``nvcr.io/nvidia/ai-dynamo/aiperf`` image (which already ships
    the ``aiperf`` executable, at a path unrelated to any host/venv layout),
    while every other tool keeps using ``python:3.12-slim``, where the
    backend runner auto-provisions the executable if not already present.
    ``SIMULATION_INCLUSTER_IMAGE`` unconditionally overrides both defaults
    (e.g. for testing); ``SIMULATION_AIPERF_INCLUSTER_IMAGE`` overrides only
    the aiperf default.
    """
    override = os.environ.get(_IMAGE_ENV_VAR, "").strip()
    if override:
        return override
    if tool_name == "aiperf":
        return os.environ.get(_AIPERF_IMAGE_ENV_VAR, "").strip() or _DEFAULT_AIPERF_INCLUSTER_IMAGE
    return _DEFAULT_INCLUSTER_IMAGE


#: Emitted (to the exec stream) immediately before the real backend command
#: starts, once any on-demand pod-side install has finished. Detected on the
#: host side to anchor the "tool ready" timestamp/elapsed-time clock instead
#: of using the moment the pod/staging process merely begins.
_TOOL_READY_SENTINEL = "__llm_d_lens_tool_ready__"


def _pod_exec_command(executable: str, args: Sequence[str], *, executable_copied: bool) -> str:
    """Build the command string to run inside the pod, installing tools on demand if needed.

    ``executable`` is resolved on the *host* (e.g. ``<venv>/bin/aiperf``) and is
    only meaningful inside the pod if that exact path was copied there
    (``executable_copied=True``, e.g. it lives in the shared backend cache that
    is bulk-copied, or was individually staged because the host already had
    it). When the host doesn't have the executable, no such path exists in the
    pod either, so the on-demand installer must place the tool on ``PATH`` and
    the command must invoke it *by name* — the host's absolute path would
    never resolve inside the pod.
    """
    arg_list = [str(argument) for argument in args]
    bootstrap = "export PATH=/root/.local/bin:$PATH; "
    tool_name = Path(executable).name
    ready_marker = f"printf '%s\\n' {shlex.quote(_TOOL_READY_SENTINEL)} 1>&2; "
    if executable_copied:
        full_cmd = shlex.join([executable, *arg_list])
        return f"{bootstrap}{ready_marker}{full_cmd}"
    if tool_name == "aiperf":
        # The official aiperf image already has this on PATH -- this branch
        # only self-installs via pip as a fallback for callers who override
        # the in-cluster image back to a bare Python one.
        full_cmd = shlex.join(["aiperf", *arg_list])
        bootstrap += (
            "if ! command -v aiperf >/dev/null 2>&1; then "
            "  pip install --no-cache-dir --disable-pip-version-check aiperf==0.12.0 "
            "|| pip install --no-cache-dir aiperf; "
            "fi; "
        )
        return f"{bootstrap}{ready_marker}{full_cmd}"
    if tool_name == "trace-replayer":
        full_cmd = shlex.join(["trace-replayer", *arg_list])
        bootstrap += (
            "if ! command -v trace-replayer >/dev/null 2>&1; then "
            "  echo 'trace-replayer executable was not staged into the pod and is not installable via pip; "
            "build it on the host first (cargo/git) so it can be cached and copied in' >&2; exit 127; "
            "fi; "
        )
        return f"{bootstrap}{ready_marker}{full_cmd}"
    full_cmd = shlex.join([executable, *arg_list])
    return f"{bootstrap}{ready_marker}{full_cmd}"


def _pod_name(task_id: str) -> str:
    return f"llm-d-lens-sim-{task_id}-{uuid.uuid4().hex[:6]}"


def resolve_incluster_target(task: SimulationTask) -> tuple[str, str | None] | None:
    """Return ``(namespace, cluster_id)`` for a task that should run in-pod, else ``None``.

    Only "deployment" endpoint tasks with a known namespace qualify; every other
    endpoint mode keeps running the backend as a local host subprocess exactly
    as before.
    """
    if task.endpoint_mode != "deployment" or not task.endpoint_namespace:
        return None
    return task.endpoint_namespace, task.endpoint_cluster_id


def _cluster_proxy_env(cluster_id: str | None = None) -> list[dict[str, str]]:
    """Return HTTP(S)_PROXY / NO_PROXY container env vars for a simulation pod.

    The target cluster's saved proxy configuration takes precedence (it is what
    the user configured for that cluster); ``resolve_proxy_env`` already falls
    back to this backend process's environment for ``auto``/unknown clusters.
    """
    resolved: dict[str, str] = {}
    if cluster_id:
        try:
            from llm_d_bench.cluster.service import resolve_proxy_env

            resolved = resolve_proxy_env(cluster_id)
        except Exception:  # pragma: no cover - simulation must not fail on proxy lookup
            resolved = {}
    env_vars = []
    http_proxy = (resolved.get("HTTP_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip()
    https_proxy = (resolved.get("HTTPS_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()
    no_proxy = (resolved.get("NO_PROXY") or os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").strip()
    no_proxy_entries = [entry.strip() for entry in no_proxy.split(",") if entry.strip()]
    for entry in ("localhost", "127.0.0.1", ".svc", ".cluster.local", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        if entry not in no_proxy_entries:
            no_proxy_entries.append(entry)
    normalized_no_proxy = ",".join(no_proxy_entries)

    for name, val in [
        ("HTTP_PROXY", http_proxy),
        ("http_proxy", http_proxy),
        ("HTTPS_PROXY", https_proxy),
        ("https_proxy", https_proxy),
        ("NO_PROXY", normalized_no_proxy),
        ("no_proxy", normalized_no_proxy),
    ]:
        if val:
            env_vars.append({"name": name, "value": val})
    return env_vars


def _pod_manifest(
    name: str,
    namespace: str,
    image: str,
    timeout_seconds: int,
    cluster_id: str | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> dict:
    container: dict = {
        "name": "runner",
        "image": image,
        # Idle until we exec the real command in; bounded so a
        # leaked pod cannot run forever if cleanup fails.
        "command": ["sleep", str(int(timeout_seconds) + 900)],
        # Task/trace/cache directories are staged at host-identical absolute
        # paths (e.g. under /home or /var/lib) regardless of which image is
        # in use, so every image must accept arbitrary mkdir/copy targets.
        # ``python:3.12-slim`` runs as root by default already; images that
        # default to a non-root user (e.g. the official aiperf image) need
        # this override to keep that working.
        "securityContext": {"runAsUser": 0, "runAsGroup": 0},
    }
    # Authenticate HuggingFace downloads with the cluster's token. Deploy copies
    # the cluster's wizard-created secret into this namespace as ``llm-d-hf-token``
    # (see deploy/service.py); reuse the same secret/key, optional so a namespace
    # without one still runs (unauthenticated, as before).
    container["env"] = [
        {
            "name": name,
            "valueFrom": {"secretKeyRef": {"name": "llm-d-hf-token", "key": "HF_TOKEN", "optional": True}},
        }
        for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")
    ] + _cluster_proxy_env(cluster_id) + [{"name": key, "value": value} for key, value in (extra_env or {}).items()]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "llm-d-lens",
                "llm-d-lens/role": _POD_LABEL,
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 5,
            "containers": [container],
        },
    }


class _PodHandle:
    def __init__(self, name: str, namespace: str, cluster_id: str | None) -> None:
        self.name = name
        self.namespace = namespace
        self.cluster_id = cluster_id

    @property
    def ref(self) -> str:
        return f"{self.namespace}/{self.name}"


async def _create_pod(
    namespace: str,
    cluster_id: str | None,
    image: str,
    timeout_seconds: int,
    extra_env: Mapping[str, str] | None = None,
) -> _PodHandle:
    name = _pod_name(uuid.uuid4().hex[:8])
    manifest = _pod_manifest(name, namespace, image, timeout_seconds, cluster_id=cluster_id, extra_env=extra_env)
    runner = scoped_runner(cluster_id)
    result = await runner.run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest), timeout=30)
    if result.returncode != 0:
        raise SimulationExecutionError(f"Failed to launch in-cluster simulation pod: {result.stderr.strip()}")
    return _PodHandle(name, namespace, cluster_id)


async def _wait_pod_running(pod: _PodHandle, context: RunContext) -> None:
    runner = scoped_runner(pod.cluster_id)
    deadline = asyncio.get_running_loop().time() + _POD_STARTUP_TIMEOUT_SECONDS
    while True:
        if context.cancel_event.is_set():
            raise SimulationCancelledError("Simulation was cancelled")
        result = await runner.run(
            ["kubectl", "get", "pod", pod.name, "-n", pod.namespace, "-o", "jsonpath={.status.phase}"],
            timeout=15,
        )
        phase = (result.stdout or "").strip()
        if phase == "Running":
            return
        if phase in {"Failed", "Succeeded"}:
            raise SimulationExecutionError(f"In-cluster simulation pod {pod.ref} exited early ({phase})")
        if asyncio.get_running_loop().time() >= deadline:
            raise SimulationExecutionError(
                f"In-cluster simulation pod {pod.ref} did not start within {_POD_STARTUP_TIMEOUT_SECONDS}s"
            )
        await asyncio.sleep(_POD_STARTUP_POLL_SECONDS)


async def _delete_pod(pod: _PodHandle, context: RunContext | None = None) -> None:
    runner = scoped_runner(pod.cluster_id)
    try:
        result = await runner.run(
            ["kubectl", "delete", "pod", pod.name, "-n", pod.namespace, "--ignore-not-found", "--wait=false"],
            timeout=30,
        )
        if result.returncode != 0 and context is not None:
            context.log(f"Warning: failed to delete in-cluster simulation pod {pod.ref}: {result.stderr.strip()}")
    except Exception as error:  # noqa: BLE001 - cleanup must never raise
        if context is not None:
            context.log(f"Warning: failed to delete in-cluster simulation pod {pod.ref}: {error}")


async def _pod_mkdirs(pod: _PodHandle, directories: Sequence[Path]) -> None:
    runner = scoped_runner(pod.cluster_id)
    quoted = " ".join(shlex.quote(str(directory)) for directory in directories)
    result = await runner.run(
        ["kubectl", "exec", "-n", pod.namespace, pod.name, "--", "mkdir", "-p", *[str(d) for d in directories]],
        timeout=30,
    )
    if result.returncode != 0:
        raise SimulationExecutionError(
            f"Failed to prepare directories {quoted} in in-cluster simulation pod {pod.ref}: {result.stderr.strip()}"
        )


async def _copy_into_pod(pod: _PodHandle, local_path: Path, context: RunContext, *, required: bool) -> None:
    if not local_path.exists():
        if required:
            raise SimulationConfigurationError(f"Expected local path {local_path} does not exist")
        return
    runner = scoped_runner(pod.cluster_id)
    # ``kubectl cp src pod:dest`` always re-creates ``basename(src)`` under
    # ``dest`` (it tars starting at src's parent and untars at dest). Passing
    # ``local_path`` itself as the destination therefore double-nests the
    # copy whenever that path already exists in the pod (e.g. because
    # ``_pod_mkdirs`` pre-created it), landing files at
    # ``<local_path>/<basename(local_path)>/...`` instead of directly inside
    # ``<local_path>``. Targeting the *parent* directory instead makes the
    # destination match the source layout exactly, for both files and dirs.
    result = await runner.run(
        ["kubectl", "cp", str(local_path), f"{pod.ref}:{local_path.parent}"],
        timeout=300,
    )
    if result.returncode != 0:
        message = f"Failed to copy {local_path} into in-cluster simulation pod {pod.ref}: {result.stderr.strip()}"
        if required:
            raise SimulationExecutionError(message)
        context.log(f"Warning: {message}")


async def _copy_out_of_pod(pod: _PodHandle, remote_path: Path, local_path: Path, context: RunContext) -> bool:
    local_path.mkdir(parents=True, exist_ok=True)
    runner = scoped_runner(pod.cluster_id)
    result = await runner.run(
        ["kubectl", "cp", f"{pod.ref}:{remote_path}", str(local_path)],
        timeout=300,
    )
    if result.returncode != 0:
        context.log(
            f"Warning: failed to copy simulation artifacts back from {pod.ref}:{remote_path}: {result.stderr.strip()}"
        )
    return result.returncode == 0


async def _copy_stream_watching_sentinel(
    stream: asyncio.StreamReader,
    destination,
    log: Callable[[str], None],
    sentinel: str,
    on_sentinel: Callable[[], None],
) -> None:
    """Like :func:`_copy_stream`, but detects ``sentinel`` and fires
    ``on_sentinel`` once when seen, without forwarding that marker line to
    ``log`` (it's an internal readiness signal, not simulation output).
    """
    seen = False
    while chunk := await stream.read(64 * 1024):
        destination.write(chunk)
        destination.flush()
        text = chunk.decode("utf-8", errors="replace")
        if not seen and sentinel in text:
            seen = True
            on_sentinel()
        message = "\n".join(line for line in text.rstrip("\n").splitlines() if sentinel not in line)
        if message:
            log(message)


async def execute_command_in_pod(
    *,
    namespace: str,
    cluster_id: str | None,
    task_dir: Path,
    executable: str,
    args: Sequence[str],
    artifact_dir: Path,
    timeout_seconds: int,
    context: RunContext,
    progress_request_counts: Callable[[], tuple[int, int | None] | None] | None = None,
    tolerate_timeout: bool = False,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run a backend command inside a Pod in ``namespace``, mirroring :func:`execute_command`.

    ``task_dir`` (which contains ``artifact_dir``) and the shared trace data
    root are copied into the pod at their host-identical absolute paths before
    the command runs, so command arguments built for local execution (which
    reference those absolute paths) work unchanged inside the pod. Artifacts
    are copied back afterwards so downstream parsing reads local disk exactly
    as it does for host-side runs.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifact_dir / "stdout.log"
    stderr_path = artifact_dir / "stderr.log"
    tool_name = Path(executable).name
    image = incluster_image(tool_name)
    started_at = utc_now()

    context.log(f"Launching in-cluster simulation pod in namespace {namespace}")
    pod = await _create_pod(namespace, cluster_id, image, timeout_seconds, extra_env=env)
    try:
        await _wait_pod_running(pod, context)
        context.log(f"In-cluster simulation pod {pod.ref} is running; staging task data")
        trace_root = BaseTrace.trace_root()
        tok_root = storage_path("cache", "tokenizers")
        backend_cache = storage_path("cache", "backends")
        # aiperf always runs from the dedicated image's own pre-installed
        # copy (see `incluster_image`/`_pod_exec_command`), never from a
        # host-copied file: a host venv's ``aiperf`` console-script has a
        # shebang pointing at that venv's own Python interpreter, which
        # doesn't exist inside the image, so copying it in would break it.
        exec_path = (
            Path(executable).expanduser().resolve() if tool_name != "aiperf" and Path(executable).is_file() else None
        )

        await _pod_mkdirs(pod, [task_dir, trace_root, tok_root, backend_cache])
        await _copy_into_pod(pod, task_dir, context, required=True)
        await _copy_into_pod(pod, trace_root, context, required=False)
        await _copy_into_pod(pod, tok_root, context, required=False)
        await _copy_into_pod(pod, backend_cache, context, required=False)
        if exec_path and not str(exec_path).startswith(str(backend_cache)) and not str(exec_path).startswith("/usr"):
            await _pod_mkdirs(pod, [exec_path.parent])
            await _copy_into_pod(pod, exec_path, context, required=False)

        # The host-resolved executable path is only usable inside the pod when
        # it was actually staged there: either individually copied above, or
        # already covered by the bulk backend-cache copy, or a system path
        # assumed to exist in the (same-family) container image. If the host
        # doesn't have the executable at all, the pod-side installer must
        # place it on PATH and the command must invoke it by bare name.
        executable_copied = exec_path is not None
        remote_command = _pod_exec_command(executable, args, executable_copied=executable_copied)
        exec_argv = [
            "kubectl",
            "exec",
            "-i",
            "-n",
            namespace,
            pod.name,
            "--",
            "sh",
            "-c",
            remote_command,
        ]
        env = kubeconfig_environment(cluster_id)
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                process = await spawn(
                    exec_argv,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as error:
                raise SimulationExecutionError(str(error)) from error
            context.process = process
            assert process.stdout is not None and process.stderr is not None
            ready_event = asyncio.Event()

            def _on_ready() -> None:
                if not ready_event.is_set():
                    ready_event.set()
                    if context.on_ready is not None:
                        context.on_ready()

            stdout_task = asyncio.create_task(_copy_stream(process.stdout, stdout, context.log))
            stderr_task = asyncio.create_task(
                _copy_stream_watching_sentinel(process.stderr, stderr, context.log, _TOOL_READY_SENTINEL, _on_ready)
            )
            exit_task = asyncio.create_task(process.wait())
            cancel_task = asyncio.create_task(context.cancel_event.wait())
            timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
            sync_task: asyncio.Task | None = None
            if progress_request_counts is not None:
                # Progress/live-metrics computation (``progress_request_counts``,
                # and any dashboard reading task artifacts while it runs) reads
                # ``artifact_dir`` on the *host*. Unlike host-side execution,
                # where the backend writes its result files directly onto local
                # disk as it runs, in-pod execution only produces those files
                # inside the pod's filesystem. Without periodically copying
                # them back, the host sees an empty artifact directory for the
                # entire run — progress stays stuck at 0% and no live metrics
                # are ever available until the single copy-back at the end.
                async def sync_partial_artifacts() -> None:
                    await ready_event.wait()
                    while True:
                        await asyncio.sleep(5)
                        # Periodic artifact sync must never abort the run.
                        with contextlib.suppress(Exception):
                            await _copy_out_of_pod(pod, artifact_dir, artifact_dir, context)

                sync_task = asyncio.create_task(sync_partial_artifacts())
            progress_task: asyncio.Task | None = None
            if progress_request_counts is not None:

                progress_task = asyncio.create_task(report_request_progress(
                    context, progress_request_counts, ready_event=ready_event,
                ))

            try:
                done, _ = await asyncio.wait(
                    {exit_task, cancel_task, timeout_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done and cancel_task.result():
                    await terminate_process(process)
                    await asyncio.gather(stdout_task, stderr_task)
                    raise SimulationCancelledError("Simulation was cancelled")
                if timeout_task in done:
                    await terminate_process(process)
                    await asyncio.gather(stdout_task, stderr_task)
                    if tolerate_timeout:
                        context.artifacts_incomplete = True
                        return CommandResult(
                            return_code=process.returncode if process.returncode is not None else -1,
                            started_at=started_at,
                            completed_at=utc_now(),
                            stdout_path=stdout_path,
                            stderr_path=stderr_path,
                            timed_out=True,
                        )
                    raise SimulationExecutionError(f"Simulation timed out after {timeout_seconds} seconds")
                return_code = exit_task.result()
                await asyncio.gather(stdout_task, stderr_task)
                if return_code != 0:
                    tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                    raise SimulationExecutionError(f"Simulation exited with code {return_code}: {tail}")
                return CommandResult(
                    return_code=return_code,
                    started_at=started_at,
                    completed_at=utc_now(),
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
            except asyncio.CancelledError:
                await terminate_process(process)
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                raise
            finally:
                for task in (exit_task, cancel_task, timeout_task):
                    if not task.done():
                        task.cancel()
                if progress_task is not None:
                    progress_task.cancel()
                if sync_task is not None:
                    sync_task.cancel()
                await asyncio.gather(
                    exit_task,
                    cancel_task,
                    timeout_task,
                    *((progress_task,) if progress_task is not None else ()),
                    *((sync_task,) if sync_task is not None else ()),
                    return_exceptions=True,
                )
                context.process = None
    finally:
        try:
            collected = await _copy_out_of_pod(pod, artifact_dir, artifact_dir, context)
            context.artifacts_incomplete = context.artifacts_incomplete or not collected
        except Exception as error:
            context.artifacts_incomplete = True
            context.log(f"Warning: final simulation artifact collection failed: {error}")
        finally:
            await _delete_pod(pod, context)
