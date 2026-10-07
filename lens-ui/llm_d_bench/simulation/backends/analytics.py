"""Backend-neutral result and timeline utilities."""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..errors import SimulationResultParseError
from ..models import (
    LatencyMetric,
    SequenceLengthKind,
    SequenceLengthSource,
    SimulationGoodputTimelinePoint,
    SimulationLatencyHeatmap,
)


def number(value: Any, fallback: float = 0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def distribution(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)

    def percentile(value: int) -> float:
        index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * value / 100) - 1))
        return ordered[index]

    return {
        "mean_ms": sum(values) / len(values),
        "p50_ms": percentile(50),
        "p90_ms": percentile(90),
        "p95_ms": percentile(95),
        "p99_ms": percentile(99),
        "max_ms": ordered[-1],
    }


def status_code_breakdown(counts: Counter[int | None]) -> list[dict]:
    total = sum(counts.values())
    return [
        {
            "status_code": status_code,
            "count": count,
            "percentage": count / total * 100 if total else 0,
        }
        for status_code, count in sorted(counts.items(), key=lambda item: (item[0] is None, item[0] or 0))
    ]


def completion_timeline(
    completion_seconds: list[float],
    *,
    arrival_seconds: list[float] | None = None,
    successful_completion_seconds: list[float] | None = None,
    failed_completion_seconds: list[float] | None = None,
    client_timeout_completion_seconds: list[float] | None = None,
    backend_error_completion_seconds: list[float] | None = None,
    origin_seconds: float = 0,
    maximum_bins: int = 120,
) -> list[dict]:
    completions = sorted(
        value - origin_seconds for value in completion_seconds if math.isfinite(value) and value >= origin_seconds
    )
    arrivals = sorted(
        value - origin_seconds for value in (arrival_seconds or []) if math.isfinite(value) and value >= origin_seconds
    )
    values = completions + arrivals
    if not values:
        return []
    duration = max(max(values), 0.001)
    bin_count = min(maximum_bins, max(1, math.ceil(duration)))
    width = duration / bin_count
    completed_counts = [0] * bin_count
    successful_counts = [0] * bin_count
    failed_counts = [0] * bin_count
    client_timeout_counts = [0] * bin_count
    backend_error_counts = [0] * bin_count
    arrival_counts = [0] * bin_count
    for value in completions:
        completed_counts[min(bin_count - 1, int(value / width))] += 1
    for raw_values, counts in (
        (
            completion_seconds if successful_completion_seconds is None else successful_completion_seconds,
            successful_counts,
        ),
        (failed_completion_seconds or [], failed_counts),
        (client_timeout_completion_seconds or [], client_timeout_counts),
        (backend_error_completion_seconds or [], backend_error_counts),
    ):
        for raw_value in raw_values:
            value = raw_value - origin_seconds
            if math.isfinite(value) and value >= 0:
                counts[min(bin_count - 1, int(value / width))] += 1
    for value in arrivals:
        arrival_counts[min(bin_count - 1, int(value / width))] += 1
    cumulative_completed = cumulative_arrived = 0
    timeline = []
    for index, completed_count in enumerate(completed_counts):
        arrival_count = arrival_counts[index]
        cumulative_completed += completed_count
        cumulative_arrived += arrival_count
        timeline.append(
            {
                "start_seconds": index * width,
                "end_seconds": duration if index == bin_count - 1 else (index + 1) * width,
                "arrived_requests": arrival_count,
                "completed_requests": completed_count,
                "successful_requests": successful_counts[index],
                "failed_requests": failed_counts[index],
                "client_timeout_requests": client_timeout_counts[index],
                "backend_error_requests": backend_error_counts[index],
                "cumulative_arrived": cumulative_arrived,
                "cumulative_completed": cumulative_completed,
            }
        )
    return timeline


def latency_timeline(
    records: list[tuple[float, float]],
    *,
    arrival_seconds: list[float] | None = None,
    origin_seconds: float = 0,
    end_seconds: float | None = None,
    maximum_bins: int = 120,
) -> list[dict]:
    values = [
        (arrived - origin_seconds, latency_ms)
        for arrived, latency_ms in records
        if math.isfinite(arrived) and arrived >= origin_seconds and math.isfinite(latency_ms) and latency_ms >= 0
    ]
    arrivals = [
        arrived - origin_seconds
        for arrived in (arrival_seconds or [])
        if math.isfinite(arrived) and arrived >= origin_seconds
    ]
    if not values and not arrivals:
        return []
    duration = max(
        max((arrived + latency_ms / 1000 for arrived, latency_ms in values), default=0),
        max(arrivals, default=0),
        (end_seconds - origin_seconds) if end_seconds is not None else 0,
        0.001,
    )
    bin_count = min(maximum_bins, max(1, math.ceil(duration)))
    width = duration / bin_count
    buckets: list[list[float]] = [[] for _ in range(bin_count)]
    arrival_counts = [0] * bin_count
    for arrived, latency_ms in values:
        buckets[min(bin_count - 1, int(arrived / width))].append(latency_ms)
    for arrived in arrivals:
        arrival_counts[min(bin_count - 1, int(arrived / width))] += 1
    timeline = []
    for index, bucket in enumerate(buckets):
        ordered = sorted(bucket)
        p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
        timeline.append(
            {
                "start_seconds": index * width,
                "end_seconds": duration if index == bin_count - 1 else (index + 1) * width,
                "request_count": len(ordered),
                "request_arrival_rps": arrival_counts[index] / width,
                "average_latency_ms": sum(ordered) / len(ordered) if ordered else None,
                "p95_latency_ms": ordered[p95_index] if ordered else None,
            }
        )
    return timeline


def latency_heatmap(
    records: list[tuple[float, float, float]],
    *,
    metric: LatencyMetric,
    sequence_length: SequenceLengthKind,
    sequence_length_source: SequenceLengthSource,
    origin_seconds: float = 0,
    maximum_time_bins: int = 40,
    maximum_sequence_length_bins: int = 20,
) -> SimulationLatencyHeatmap | None:
    values = [
        (arrived - origin_seconds, sequence, metric_ms)
        for arrived, sequence, metric_ms in records
        if math.isfinite(arrived)
        and arrived >= origin_seconds
        and math.isfinite(sequence)
        and sequence >= 0
        and math.isfinite(metric_ms)
        and metric_ms >= 0
    ]
    if not values:
        return None

    duration = max(max(arrived for arrived, _, _ in values), 0.001)
    time_bin_count = min(maximum_time_bins, max(1, math.ceil(duration)))
    time_width = duration / time_bin_count
    minimum_sequence = min(sequence for _, sequence, _ in values)
    maximum_sequence = max(sequence for _, sequence, _ in values)
    sequence_bin_count = (
        1
        if maximum_sequence == minimum_sequence
        else min(maximum_sequence_length_bins, max(1, math.ceil(math.sqrt(len(values)))))
    )
    sequence_width = max(1, math.ceil((maximum_sequence - minimum_sequence) / sequence_bin_count))
    buckets: dict[tuple[int, int], list[float]] = {}
    for arrived, sequence, metric_ms in values:
        time_index = min(time_bin_count - 1, int(arrived / time_width))
        sequence_index = min(sequence_bin_count - 1, int((sequence - minimum_sequence) / sequence_width))
        buckets.setdefault((time_index, sequence_index), []).append(metric_ms)

    cells = []
    averages = []
    for (time_index, sequence_index), bucket in sorted(buckets.items()):
        average = sum(bucket) / len(bucket)
        averages.append(average)
        cells.append(
            {
                "start_seconds": time_index * time_width,
                "end_seconds": duration if time_index == time_bin_count - 1 else (time_index + 1) * time_width,
                "sequence_length_start": math.floor(minimum_sequence + sequence_index * sequence_width),
                "sequence_length_end": math.ceil(
                    min(maximum_sequence + 1, minimum_sequence + (sequence_index + 1) * sequence_width)
                ),
                "request_count": len(bucket),
                "average_metric_ms": average,
            }
        )
    return SimulationLatencyHeatmap(
        metric=metric,
        sequence_length=sequence_length,
        sequence_length_source=sequence_length_source,
        time_bin_count=time_bin_count,
        sequence_length_bin_count=sequence_bin_count,
        minimum_metric_ms=min(averages),
        maximum_metric_ms=max(averages),
        cells=cells,
    )


def rate_timelines(
    records: list[tuple[float, float, float, bool]],
    *,
    origin_seconds: float = 0,
    maximum_bins: int = 120,
) -> tuple[list[dict], list[dict]]:
    values = [
        (arrived - origin_seconds, completed - origin_seconds, output_tokens, failed)
        for arrived, completed, output_tokens, failed in records
        if math.isfinite(arrived)
        and arrived >= origin_seconds
        and math.isfinite(completed)
        and completed >= origin_seconds
    ]
    if not values:
        return [], []
    duration = max(max(max(arrived, completed) for arrived, completed, _, _ in values), 0.001)
    bin_count = min(maximum_bins, max(1, math.ceil(duration)))
    width = duration / bin_count
    arrival_counts = [0] * bin_count
    completion_buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bin_count)]
    for arrived, completed, output_tokens, failed in values:
        arrival_counts[min(bin_count - 1, int(arrived / width))] += 1
        completion_buckets[min(bin_count - 1, int(completed / width))].append((max(0, output_tokens), failed))
    throughput = []
    errors = []
    cumulative_failures = 0
    for index, bucket in enumerate(completion_buckets):
        request_count = len(bucket)
        failed_count = sum(1 for _, failed in bucket if failed)
        output_tokens = sum(tokens for tokens, _ in bucket)
        cumulative_failures += failed_count
        bounds = {
            "start_seconds": index * width,
            "end_seconds": duration if index == bin_count - 1 else (index + 1) * width,
        }
        throughput.append(
            {
                **bounds,
                "request_arrival_rps": arrival_counts[index] / width,
                "request_completion_rps": request_count / width,
                "token_throughput_tps": output_tokens / width,
                "arrived_requests": arrival_counts[index],
                "completed_requests": request_count,
                "output_tokens": output_tokens,
            }
        )
        errors.append(
            {
                **bounds,
                "failed_requests": failed_count,
                "error_rate_percent": failed_count / request_count * 100 if request_count else 0,
                "cumulative_failures": cumulative_failures,
            }
        )
    return throughput, errors


def goodput_timeline(
    records: list[tuple[float, bool]],
    *,
    origin_seconds: float = 0,
    maximum_bins: int = 120,
) -> list[SimulationGoodputTimelinePoint]:
    values = [
        (completed - origin_seconds, qualified)
        for completed, qualified in records
        if math.isfinite(completed) and completed >= origin_seconds
    ]
    if not values:
        return []
    duration = max(max(completed for completed, _ in values), 0.001)
    bin_count = min(maximum_bins, max(1, math.ceil(duration)))
    width = duration / bin_count
    buckets: list[list[bool]] = [[] for _ in range(bin_count)]
    for completed, qualified in values:
        buckets[min(bin_count - 1, int(completed / width))].append(qualified)
    return [
        SimulationGoodputTimelinePoint(
            start_seconds=index * width,
            end_seconds=duration if index == bin_count - 1 else (index + 1) * width,
            completed_requests=len(bucket),
            good_requests=sum(bucket),
            request_throughput_rps=len(bucket) / width,
            goodput_rps=sum(bucket) / width,
        )
        for index, bucket in enumerate(buckets)
    ]


def read_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError("must be a JSON object")
        return value
    except Exception as error:
        raise SimulationResultParseError(f"{label}: {error}") from error


def visit_json_lines(
    path: Path,
    label: str,
    visit: Callable[[dict], None],
    *,
    tolerate_incomplete: bool = False,
) -> None:
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError("record is not an object")
                visit(value)
            except Exception as error:
                if tolerate_incomplete:
                    continue
                raise SimulationResultParseError(f"{label} at line {index}: {error}") from error


def artifact_error_message(record: dict, value: Any) -> str | None:
    """Format a selected tool error without changing caller-specific field precedence."""
    if isinstance(value, dict):
        for key in ("message", "detail", "type", "code"):
            if value.get(key):
                return str(value[key])[:1000]
        return json.dumps(value, ensure_ascii=True)[:1000]
    if value:
        return str(value)[:1000]
    for key in ("error_message", "message", "detail", "response_body"):
        if record.get(key):
            return str(record[key])[:1000]
    return None
