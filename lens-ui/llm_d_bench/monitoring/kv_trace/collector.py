"""Bounded collection from already instrumented pods, independent of ownership.

Only begin/end capture sessions are requested. This collector never installs
instrumentation, restarts model containers, or changes deployment manifests.
"""

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .manifest import LABEL, MOUNT


def utc(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def client_request_ids(summary_path):
    """Use response IDs to exclude health checks, warmup and unrelated traffic."""
    rows = json.loads(Path(summary_path).with_name("per_request_lifecycle_metrics.json").read_text())
    identities = set()
    for row in rows:
        if "error" not in row:
            raise ValueError("Client request completion status missing.")
        if row["error"] is not None or row.get("stage_id") == -1:
            continue
        chunks = (row.get("info", {}).get("response_metrics") or {}).get("response_chunks") or []
        if not chunks:
            response = row.get("response") or ""
            chunks = [line[5:].strip() for line in response.splitlines() if line.startswith("data:")] or [response]
        ids = set()
        for chunk in chunks:
            try:
                payload = json.loads(chunk)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                ids.add(payload["id"])
        if len(ids) != 1 or identities.intersection(ids):
            raise ValueError("Unique server response IDs are required for KV request correlation.")
        identities.update(ids)
    return identities


def assemble(snapshots, expected_successes, topology_stable=True, client_ids=None):
    """Produce a trace only when engine completion coverage matches the client."""
    result = {
        "schema_version": 1,
        "complete": False,
        "dropped_events": 0,
        "stage_index": None,
        "identity_semantics": "context-prefix-hash",
        "measurement_scope": "completed-full-prompt-blocks",
        "accesses": [],
        "access_count": 0,
    }
    try:
        if not topology_stable or not snapshots:
            raise ValueError("Model pods changed, restarted, or no engine snapshots were collected.")
        if client_ids is None or len(client_ids) != expected_successes:
            raise ValueError("Client response IDs do not cover all successful requests.")
        starts, ends, identities = [], [], set()
        for pod, snapshot in snapshots:
            if snapshot["engines"] != snapshot["engines_end"]:
                raise ValueError("KV manager processes changed during measurement.")
            if not snapshot["engines"] or any(not item["supported"] for item in snapshot["engines"]):
                raise ValueError("Engine probe does not support this cache layout.")
            starts.append(snapshot["start"])
            ends.append(snapshot["end"])
            for request in snapshot["requests"]:
                engine_id = request["request_id"]
                # vLLM 0.26 assigns an internal ID by appending eight random
                # hex characters after the external ID (including its -0
                # completion branch). Never strip arbitrary suffixes.
                external_ids = {engine_id, re.sub(r"-[0-9a-f]{8}$", "", engine_id)}
                candidates = external_ids | {value[:-2] for value in external_ids if value.endswith("-0")}
                matches = candidates & client_ids
                if len(matches) > 1:
                    raise ValueError("Ambiguous engine/client request ID correlation.")
                matched = next(iter(matches), None)
                if matched is None:
                    continue
                if request.get("error"):
                    raise ValueError(f"Engine KV capture: {request['error']}")
                identity = matched
                if identity in identities:
                    raise ValueError("Duplicate engine completion records.")
                identities.add(identity)
                if not snapshot["start"] <= request["timestamp"] < snapshot["end"]:
                    raise ValueError("Engine completion is outside the capture window.")
                for access in request["accesses"]:
                    result["accesses"].append({**access, "timestamp": utc(request["timestamp"]), "pod": pod["name"]})
        if type(expected_successes) is not int or expected_successes <= 0 or len(identities) != expected_successes:
            raise ValueError(
                f"Engine/client successful request coverage differs: {len(identities)} / {expected_successes}."
            )
        result.update(
            complete=True,
            window={"start": utc(min(starts)), "end": utc(max(ends))},
            access_count=len(result["accesses"]),
            request_count=len(identities),
            publishers=[{**pod, "engines": snapshot["engines"]} for pod, snapshot in snapshots],
            source="vLLM held prompt blocks at successful request completion",
        )
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        result.update(reason=str(error), accesses=[], access_count=0)
    return result


class KVTraceCollector:
    def __init__(self, namespace, environment, enabled=False, command=None, unsupported_reason=None):
        self.namespace = namespace
        self.environment = environment
        self.enabled = enabled
        self.command = command or self._command
        self.unsupported_reason = unsupported_reason
        self.session = None
        self.started = []
        self.reason = None

    async def _command(self, *arguments):
        process = await asyncio.create_subprocess_exec(
            self.environment.get("LLM_D_BENCH_KUBECTL_PATH", "kubectl"),
            *arguments,
            env={**os.environ, **self.environment},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
            if process.returncode:
                raise ValueError(stderr.decode(errors="replace")[-1000:] or "KV probe command failed")
            return json.loads(stdout)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def discover(self):
        response = await self.command("get", "pods", "--namespace", self.namespace, "-o", "json")
        targets = []
        for pod in response.get("items", []):
            if pod.get("metadata", {}).get("deletionTimestamp"):
                continue
            for container in pod.get("spec", {}).get("containers", []):
                limits = container.get("resources", {}).get("limits", {})
                is_model = any("gpu" in key or "xpu" in key for key in limits)
                instrumented = any(env.get("name") == "PRISM_KV_TRACE_NAMESPACE" for env in container.get("env", []))
                if not instrumented and not is_model:
                    continue
                if not instrumented or pod.get("metadata", {}).get("labels", {}).get(LABEL) != "v1":
                    raise ValueError(
                        "Model deployment has no KV probe; redeploy its configuration to install instrumentation."
                    )
                status = next(
                    (s for s in pod.get("status", {}).get("containerStatuses", []) if s["name"] == container["name"]),
                    {},
                )
                if not status.get("ready"):
                    raise ValueError("Model container is not ready for KV collection.")
                targets.append(
                    {
                        "name": pod["metadata"]["name"],
                        "uid": pod["metadata"]["uid"],
                        "container": container["name"],
                        "restarts": status.get("restartCount", 0),
                    }
                )
        if not targets:
            raise ValueError("No instrumented vLLM model containers found in the benchmark namespace.")
        return sorted(targets, key=lambda item: (item["uid"], item["container"]))

    async def probe(self, pod, action):
        return await self.command(
            "exec",
            "--namespace",
            self.namespace,
            pod["name"],
            "-c",
            pod["container"],
            "--",
            "python3",
            f"{MOUNT}/prism_kv_engine.py",
            action,
            self.session,
        )

    async def begin(self, workspace):
        if not self.enabled:
            return
        self.session, self.started, self.reason = str(uuid4()), [], None
        if self.unsupported_reason:
            self.reason = self.unsupported_reason
            return
        try:
            self.targets = await self.discover()
            for pod in self.targets:
                self.started.append(pod)  # Also attempt release when a begin response is lost.
                response = await self.probe(pod, "begin")
                if response.get("error"):
                    raise ValueError(response["error"])
        except Exception as error:
            self.reason = str(error)

    async def finish(self, workspace, metrics, succeeded):
        if not self.enabled or self.session is None:
            return
        snapshots = []
        try:
            for pod in self.started:
                try:
                    snapshot = await self.probe(pod, "end")
                    if snapshot.get("error"):
                        raise ValueError(snapshot["error"])
                    snapshots.append((pod, snapshot))
                except Exception as error:
                    self.reason = str(error)
            stable = not self.reason and await self.discover() == self.targets
            if self.reason or not succeeded:
                trace = {
                    "schema_version": 1,
                    "complete": False,
                    "reason": self.reason or "Benchmark did not finish successfully.",
                }
            else:
                ids = client_request_ids(metrics["summary_path"]) if metrics.get("summary_path") else None
                trace = assemble(snapshots, metrics.get("success_count"), stable, ids)
            summary_path = metrics.get("summary_path")
            root = Path(summary_path).parent if summary_path else Path(workspace)
            self._save(root / "summary_kv_access.json", trace)
            # A single reported stage is the same cohort; never infer multiple
            # stage membership from elapsed durations.
            stages = list(root.glob("stage_*_lifecycle_metrics.json"))
            if len(stages) == 1 and stages[0].name == "stage_0_lifecycle_metrics.json":
                payload = json.loads(stages[0].read_text())
                if payload.get("successes", {}).get("count") == metrics.get("success_count"):
                    self._save(root / "stage_0_kv_access.json", {**trace, "stage_index": 0})
        except Exception as error:
            trace = {"schema_version": 1, "complete": False, "reason": str(error)}
            root = Path(metrics["summary_path"]).parent if metrics.get("summary_path") else Path(workspace)
            self._save(root / "summary_kv_access.json", trace)
        finally:
            self.session, self.started = None, []

    @staticmethod
    def _save(path, payload):
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(payload))
            temporary.replace(path)
        except OSError:
            # Missing trace stays unavailable; never fail benchmark cleanup.
            pass
