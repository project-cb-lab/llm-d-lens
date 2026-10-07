"""Bounded, live diagnostics for Kubernetes benchmark launchers."""

import asyncio
import json
import os
from contextlib import aclosing, suppress

_POLL_SECONDS = 10

#: Printed by every upstream harness script once the load generator finished,
#: before its optional post-processing/analysis step runs. Lens parses the load
#: generator's own metrics, so a later analysis failure must not fail the run.
_LOAD_GENERATION_COMPLETED = "Harness completed successfully."


async def _read(environment, *args):
    from llm_d_bench.utils.kubernetes_commands import execute_sdk_command, sdk_enabled

    if sdk_enabled():
        result = await execute_sdk_command(
            ["kubectl", *args],
            kubeconfig=environment.get("KUBECONFIG") or os.environ.get("KUBECONFIG"),
            timeout=10,
        )
        if result is not None:
            if result.returncode:
                raise RuntimeError(result.stderr)
            return result.stdout
    process = await asyncio.create_subprocess_exec(
        environment.get("LLM_D_BENCH_KUBECTL_PATH") or "kubectl",
        *args,
        env={**os.environ, **environment},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), 10)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(err.decode(errors="replace")[-2000:])
    return out.decode(errors="replace")


def pod_failure(pod):
    status = pod.get("status", {})
    for container in status.get("initContainerStatuses", []) + status.get("containerStatuses", []):
        state = container.get("state", {})
        terminated = state.get("terminated", {})
        waiting = state.get("waiting", {})
        if terminated.get("exitCode", 0) != 0:
            return f"{container['name']}: {terminated.get('reason', 'exited')} (exit {terminated['exitCode']})"
        if waiting.get("reason") in {
            "CrashLoopBackOff",
            "ImagePullBackOff",
            "ErrImagePull",
            "CreateContainerConfigError",
        }:
            return f"{container['name']}: {waiting['reason']} {waiting.get('message', '')}"
    if status.get("phase") == "Failed":
        return status.get("message") or status.get("reason") or "Pod failed"
    return None


async def _poll_harness(run, namespace, environment, save, now, live=None):
    next_poll = 0
    while True:
        refresh = asyncio.get_running_loop().time() >= next_poll
        if live is not None:
            live["changed"].clear()
        try:
            if not refresh and live is not None:
                payload = {"items": list(live["pods"].values())}
            else:
                payload = json.loads(
                    await _read(
                        environment,
                        "-n",
                        namespace,
                        "get",
                        "pods",
                        "-l",
                        "app=llmdbench-harness-launcher",
                        "-o",
                        "json",
                    )
                )
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            run["harness_warning"] = f"Cannot inspect benchmark pods: {error}"
            save(run)
            await _wait_for_pods(live, _POLL_SECONDS)
            continue
        if refresh:
            next_poll = asyncio.get_running_loop().time() + _POLL_SECONDS
        logs = []
        warning = live.get("warning") if live is not None else None
        failure = None
        for pod in payload.get("items", []):
            name = pod["metadata"]["name"]
            failure = failure or pod_failure(pod)
            # Even off a cached watch event, fetch logs for a failing pod so the
            # load-generation marker below is seen before deciding to fail.
            if not refresh and not failure:
                continue
            try:
                output = await _read(environment, "-n", namespace, "logs", name, "--all-containers=true", "--tail=80")
            except (OSError, RuntimeError, TimeoutError) as error:
                output = f"Log retrieval unavailable: {error}"
            if output:
                logs.append(f"[{name}]\n{output}")
            if "Network is unreachable" in output:
                warning = (
                    f"{name}: Network is unreachable while preparing benchmark dependencies. "
                    "Check harness network/proxy access."
                )
                failure = warning + " Benchmark stopped because the required dependency is unreachable."
        if logs:
            run["harness_logs"] = "\n".join(logs)[-16000:]
        if failure and _LOAD_GENERATION_COMPLETED in (run.get("harness_logs") or ""):
            # The load generator finished and wrote its metrics; only the
            # harness's optional post-processing (inference-perf --analyze /
            # benchmark-report) failed afterward. Lens parses the load
            # generator's own metrics, so this is a warning, not a failure.
            warning = f"{failure}; the load generator had already completed, so its metrics are used"
            failure = None
        run["harness_checked_at"] = now()
        run["harness_warning"] = warning
        save(run)
        if failure:
            raise RuntimeError(f"Benchmark harness failed: {failure}\n{run.get('harness_logs', '')[-4000:]}")
        delay = _POLL_SECONDS if live is None else max(0, next_poll - asyncio.get_running_loop().time())
        await _wait_for_pods(live, delay)


async def _wait_for_pods(live, delay):
    if live is None:
        await asyncio.sleep(delay)
        return
    with suppress(TimeoutError):
        await asyncio.wait_for(live["changed"].wait(), delay)


async def _watch_pods(namespace, environment, live):
    from llm_d_bench.utils.kubernetes_auth import CliAuthenticationRequiredError
    from llm_d_bench.utils.kubernetes_watch import watch_resource_events

    try:
        async with aclosing(
            watch_resource_events(
                "pods",
                kubeconfig=environment.get("KUBECONFIG") or os.environ.get("KUBECONFIG"),
                namespace=namespace,
                selector="app=llmdbench-harness-launcher",
            )
        ) as events:
            async for event in events:
                if event["type"] == "RESET":
                    live["pods"].clear()
                else:
                    pod = event["object"]
                    metadata = pod["metadata"]
                    key = metadata.get("uid") or metadata["name"]
                    if event["type"] == "DELETED":
                        live["pods"].pop(key, None)
                    else:
                        live["pods"][key] = pod
                live["changed"].set()
    except CliAuthenticationRequiredError:
        # Authentication policy belongs to the existing command adapter, which
        # selects kubectl for this provider; keep the periodic diagnostics path.
        return
    except Exception as error:
        # API exception strings may include response bodies or private URLs.
        live["warning"] = f"Cannot watch benchmark pods: {type(error).__name__}"
        live["changed"].set()


async def watch_harness(run, namespace, environment, save, now):
    from llm_d_bench.utils.kubernetes_commands import sdk_enabled

    if not sdk_enabled():
        return await _poll_harness(run, namespace, environment, save, now)
    live = {"pods": {}, "changed": asyncio.Event(), "warning": None}
    worker = asyncio.create_task(_watch_pods(namespace, environment, live))
    try:
        await _poll_harness(run, namespace, environment, save, now, live)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
