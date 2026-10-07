"""Strict request and KV evidence from artifacts beside a lifecycle report.

No prompts or response bodies are copied into the persisted evaluation record.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import yaml


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def constraints(targets: dict) -> dict:
    return {
        key: targets[f"{key}_ms"] / 1000
        for key in ("ttft", "tpot")
        if number(targets.get(f"{key}_ms")) is not None and targets[f"{key}_ms"] > 0
    }


def enable_request_reports(workload: dict, targets: dict) -> dict:
    report = workload.setdefault("report", {})
    report.setdefault("request_lifecycle", {}).update(summary=True, per_stage=True, per_request=True)
    if limits := constraints(targets):
        report["goodput"] = {"constraints": limits}
    return workload


def unavailable(reason: str) -> dict:
    return {"schema_version": 1, "status": "unavailable", "reason": reason}


def request_goodput(rows, metrics: dict, targets: dict, stage_index=None) -> dict:
    limits = constraints(targets)
    if not limits:
        return unavailable("Configure at least one TTFT or TPOT latency target.")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return unavailable("Per-request lifecycle records were not collected or are malformed.")
    if stage_index is not None:
        if any(type(row.get("stage_id")) is not int for row in rows):
            return unavailable(
                "Raw requests lack stage_id; requests cannot be assigned from aggregate stage durations."
            )
        rows = [row for row in rows if row["stage_id"] == stage_index]
    else:
        rows = [row for row in rows if row.get("stage_id") != -1]
    duration = number(metrics.get("benchmark_time_seconds"))
    expected = metrics.get("request_count")
    if not duration or duration <= 0 or type(expected) is not int or len(rows) != expected or not rows:
        return unavailable("Request coverage or the measured duration is incomplete.")
    good = successes = missing = empty_output = 0
    starts, ends = [], []
    identities = set()
    for row in rows:
        identity = row.get("request_id")
        if identity is not None:
            if not isinstance(identity, str) or identity in identities:
                return unavailable("Duplicate or invalid request identities.")
            identities.add(identity)
        start, end = number(row.get("start_time")), number(row.get("end_time"))
        if start is None or end is None or end < start or "error" not in row:
            return unavailable("Request timestamps or completion status are missing.")
        starts.append(start)
        ends.append(end)
        if row["error"] is not None:
            continue
        successes += 1
        info = row.get("info") or {}
        if not isinstance(info, dict):
            return unavailable("Malformed per-request info.")
        response = info.get("response_metrics") or {}
        if not isinstance(response, dict):
            return unavailable("Malformed per-request response metrics.")
        times = response.get("output_token_times") or []
        tokens = number(response.get("output_tokens"))
        if tokens == 0 and not times:
            # A recorded empty response cannot meet a token latency target,
            # even when the transport/harness labels it a success.
            empty_output += 1
            continue
        valid_times = (
            isinstance(times, list)
            and bool(times)
            and all(number(t) is not None and start <= t <= end for t in times)
            and all(a <= b for a, b in zip(times, times[1:], strict=False))
        )
        first = times[0] if valid_times else None
        values = {
            "ttft": first - start if first is not None else None,
            "tpot": (end - first) / (tokens - 1) if first is not None and tokens is not None and tokens > 1 else None,
        }
        if any(values[key] is None for key in limits):
            missing += 1
        elif all(values[key] <= limit for key, limit in limits.items()):
            good += 1
    if successes != metrics.get("success_count") or len(rows) - successes != metrics.get("failure_count"):
        return unavailable("Raw success/failure counts disagree with the lifecycle summary.")
    if max(ends) - min(starts) > duration + max(0.001, duration * 0.001):
        return unavailable("Request timestamps exceed the recorded measurement duration.")
    if missing:
        return {
            **unavailable(
                "Successful requests are missing required latency measurements (TPOT needs at least two output tokens)."
            ),
            "missing_latency_count": missing,
        }
    return {
        "schema_version": 1,
        "status": "measured",
        "value": good / duration,
        "good_requests": good,
        "request_count": len(rows),
        "success_count": successes,
        "empty_output_count": empty_output,
        "duration_seconds": duration,
        "constraints_seconds": limits,
        "window": {"start_seconds": min(starts), "end_seconds": max(ends), "clock": "harness monotonic"},
        "stage_index": stage_index,
        "source": "inference-perf per-request lifecycle",
        "formula": "successful requests meeting all configured latency targets / benchmark_time_seconds",
    }


def kv_working_set(payload, stage_index=None) -> dict:
    """Consume a complete, request-access trace; placement events are insufficient."""
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return unavailable("No versioned KV block access trace was collected.")
    if payload.get("complete") is not True and payload.get("reason"):
        return unavailable(str(payload["reason"]))
    if (
        payload.get("complete") is not True
        or payload.get("dropped_events") != 0
        or payload.get("stage_index") != stage_index
        or payload.get("identity_semantics") != "context-prefix-hash"
    ):
        return unavailable("KV access coverage, stage identity or context-aware block identities are unverified.")
    try:
        window = payload["window"]
        start = datetime.fromisoformat(window["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(window["end"].replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("window")
        accesses = payload["accesses"]
        if (
            not isinstance(accesses, list)
            or type(payload.get("access_count")) is not int
            or payload["access_count"] != len(accesses)
        ):
            raise ValueError("coverage")
        blocks = {}
        for access in accesses:
            timestamp = datetime.fromisoformat(access["timestamp"].replace("Z", "+00:00"))
            if not start <= timestamp < end:
                raise ValueError("access outside window")
            identity = (access["namespace"], access["block_hash"])
            tokens = access["valid_tokens"]
            if (
                any(not isinstance(part, str) or not part for part in identity)
                or type(tokens) is not int
                or tokens <= 0
            ):
                raise ValueError("identity or token count")
            if identity in blocks and blocks[identity] != tokens:
                raise ValueError("conflicting block size")
            blocks[identity] = tokens
        return {
            "schema_version": 1,
            "status": "measured",
            "value": sum(blocks.values()),
            "unique_blocks": len(blocks),
            "access_count": len(accesses),
            "window": window,
            "stage_index": stage_index,
            "source": payload.get("source", "complete KV block access trace"),
            "measurement_scope": payload.get("measurement_scope"),
            "request_count": payload.get("request_count"),
            "formula": "sum(unique full prompt block tokens held by correlated successful requests)"
            if payload.get("measurement_scope") == "completed-full-prompt-blocks"
            else "sum(valid tokens of unique context-prefix blocks accessed in [start, end))",
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        return unavailable("KV access trace has invalid timestamps, coverage or conflicting block identities.")


def apply_request_evidence(record: dict) -> bool:
    """Backfill exact per-scope artifacts; never search other runs or warmup output."""
    changed = False
    cache = {}

    def read(path):
        if path not in cache:
            try:
                cache[path] = (
                    json.loads(path.read_text()) if path.suffix == ".json" else yaml.safe_load(path.read_text())
                )
            except (OSError, ValueError, yaml.YAMLError):
                cache[path] = None
        return cache[path]

    def apply(metrics, stage_index=None):
        nonlocal changed
        if not isinstance(metrics, dict) or not isinstance(metrics.get("summary_path"), str):
            return
        summary = Path(metrics["summary_path"])
        if not summary.is_file():
            return  # Persisted computed evidence remains valid after artifact cleanup.
        limits = constraints(record.get("sla_targets") or {})
        raw_path = summary.parent / "per_request_lifecycle_metrics.json"
        prefix = "summary" if stage_index is None else f"stage_{stage_index}"
        kv_path = summary.parent / f"{prefix}_kv_access.json"
        # Detail polling must not repeatedly parse large request/response files.
        sources = []
        for path in (summary, raw_path, summary.parent / "config.yaml", kv_path):
            try:
                stat = path.stat()
                sources.append((str(path), stat.st_size, stat.st_mtime_ns))
            except OSError:
                sources.append((str(path), None, None))
        fingerprint = sha256(
            json.dumps(
                [
                    1,
                    stage_index,
                    limits,
                    sources,
                    [
                        metrics.get(key)
                        for key in ("request_count", "success_count", "failure_count", "benchmark_time_seconds")
                    ],
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if (
            metrics.get("request_evidence_fingerprint") == fingerprint
            and "request_slo_goodput" in metrics
            and "distinct_kv_working_set" in metrics
        ):
            return
        rows = read(raw_path) if limits else None
        evidence = request_goodput(rows, metrics, record.get("sla_targets") or {}, stage_index)
        # Older harnesses omit stage_id in raw reports. Their native per-stage
        # good-request counter is usable with the saved effective config.
        if evidence["status"] != "measured" and limits:
            payload = read(summary)
            config = read(summary.parent / "config.yaml")
            if isinstance(payload, dict) and isinstance(config, dict):
                report = config.get("report") or {}
                if not isinstance(report, dict):
                    report = {}
                configured = mapping(report.get("goodput")).get("constraints")
                api = mapping(config.get("api"))
                headers = {str(k).lower() for k in mapping(api.get("headers"))}
                overrides = any(
                    str(api.get(key, default)).lower() in headers
                    for key, default in (("slo_ttft_header", "x-slo-ttft-ms"), ("slo_tpot_header", "x-slo-tpot-ms"))
                )
                goodput = mapping(mapping(payload.get("successes")).get("goodput_metrics"))
                good, total = goodput.get("good_requests"), goodput.get("total_requests")
                duration = number(metrics.get("benchmark_time_seconds"))
                # Do not mask incomplete raw measurements with a summary fallback.
                scope_missing = rows is None or (
                    stage_index is not None
                    and isinstance(rows, list)
                    and all(isinstance(row, dict) and "stage_id" not in row for row in rows)
                )
                if (
                    scope_missing
                    and configured == limits
                    and not overrides
                    and type(good) is int
                    and type(total) is int
                    and 0 <= good <= total
                    and total == metrics.get("success_count")
                    and duration is not None
                    and duration > 0
                ):
                    evidence = {
                        "schema_version": 1,
                        "status": "measured",
                        "value": good / duration,
                        "good_requests": good,
                        "success_count": total,
                        "request_count": metrics.get("request_count"),
                        "duration_seconds": duration,
                        "constraints_seconds": limits,
                        "stage_index": stage_index,
                        "source": "inference-perf native per-request SLO counter",
                        "formula": "native good_requests / full lifecycle benchmark_time_seconds",
                    }
        evidence["artifact_path"] = (
            str(summary.parent / "per_request_lifecycle_metrics.json")
            if evidence.get("source") == "inference-perf per-request lifecycle"
            else str(summary)
        )
        saved_kv = mapping(metrics.get("distinct_kv_working_set"))
        kv = (
            saved_kv
            if not kv_path.exists() and saved_kv.get("status") == "measured"
            else kv_working_set(read(kv_path), stage_index)
        )
        kv["artifact_path"] = str(kv_path)
        for key, value in (("request_slo_goodput", evidence), ("distinct_kv_working_set", kv)):
            if metrics.get(key) != value:
                metrics[key] = value
                changed = True
        if metrics.get("request_evidence_fingerprint") != fingerprint:
            metrics["request_evidence_fingerprint"] = fingerprint
            changed = True

    if record.get("matrix_results"):
        for point in record["matrix_results"]:
            apply(point.get("metrics"))
            for index, stage in enumerate(point.get("stage_metrics") or []):
                apply(stage.get("metrics"), index)
    else:
        apply(record.get("metrics"))
        for index, stage in enumerate(record.get("rate_stage_results") or []):
            apply(stage.get("metrics"), index)
    return changed
