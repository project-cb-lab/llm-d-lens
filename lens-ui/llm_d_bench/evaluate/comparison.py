"""Matched-load comparison of persisted Evaluation measurements.

Ratios remain guide / baseline (lower latency ratios are favorable). Missing,
non-finite and ambiguous measurements never establish a comparison.
"""

from __future__ import annotations

import math
from collections import Counter

METRIC_DISTRIBUTIONS = {
    "throughput_tps": None,
    "ttft_ms": "ttft",
    "tpot_ms": "tpot",
    "request_latency_ms": "request_latency",
}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _geometric_mean(values):
    values = [value for value in values if _number(value) and value >= 0]
    if not values:
        return None
    if 0 in values:
        return 0.0
    return round(math.exp(sum(math.log(value) for value in values) / len(values)), 4)


def _matched(left, right, keys):
    def key(item):
        values = tuple(item.get(name) for name in keys)
        return values if all(_number(value) and value >= 0 for value in values) else None

    left_counts = Counter(key(item) for item in left)
    right_counts = Counter(key(item) for item in right)
    right_index = {key(item): item for item in right if key(item) is not None}
    for item in left:
        identity = key(item)
        if identity is not None and left_counts[identity] == right_counts[identity] == 1:
            yield item, right_index[identity]


def _metric_values(metrics, stat):
    result = {}
    for name, distribution in METRIC_DISTRIBUTIONS.items():
        value = (
            metrics.get(name)
            if distribution is None
            else ((metrics.get("latency_distributions") or {}).get(distribution) or {}).get(f"{stat}_ms")
        )
        if _number(value):
            result[name] = value
    return result


def _aggregate(pairs, *, matrix=False):
    rows = []
    ratios = {suffix: {key: [] for key in METRIC_DISTRIBUTIONS} for suffix in ("", "_p90", "_p99")}
    grouped = {key: {} for key in ("isl", "osl", "concurrency")}
    totals = {"guide": [0.0, 0.0], "baseline": [0.0, 0.0]}
    for dimensions, guide_stage, baseline_stage in pairs:
        metrics = {"guide": guide_stage.get("metrics") or {}, "baseline": baseline_stage.get("metrics") or {}}
        row = dict(dimensions)
        for stat, suffix in (("mean", ""), ("p90", "_p90"), ("p99", "_p99")):
            values = {side: _metric_values(data, stat) for side, data in metrics.items()}
            for side in values:
                row[side + suffix] = {name: round(value, 4) for name, value in values[side].items()}
            row["ratio" + suffix] = {}
            for name, guide in values["guide"].items():
                baseline = values["baseline"].get(name)
                if baseline is None or baseline == 0:
                    continue
                ratio = guide / baseline
                if not _number(ratio):
                    continue
                row["ratio" + suffix][name] = round(ratio, 4)
                ratios[suffix][name].append(ratio)
                if matrix and not suffix:
                    for group in grouped:
                        grouped[group].setdefault(str(row[group]), {}).setdefault(name, []).append(ratio)
        # Preserve the matrix's published absolute-value fields, including nulls.
        if matrix:
            for side, data in metrics.items():
                row[side] = {name: data.get(name) if _number(data.get(name)) else None for name in METRIC_DISTRIBUTIONS}
        rows.append(row)
        samples = {
            side: (data.get("total_output_tokens"), data.get("benchmark_time_seconds"))
            for side, data in metrics.items()
        }
        if all(
            _number(tokens) and tokens >= 0 and _number(duration) and duration > 0
            for tokens, duration in samples.values()
        ):
            for side, (tokens, duration) in samples.items():
                totals[side][0] += tokens
                totals[side][1] += duration
    if not rows:
        return None
    throughput = {side: tokens / duration if duration else None for side, (tokens, duration) in totals.items()}
    result = {
        "case_count" if matrix else "stage_count": len(rows),
        "rows": rows,
        **{
            "geometric_mean_ratio" + suffix: {
                name: mean for name, values in metrics.items() if (mean := _geometric_mean(values)) is not None
            }
            for suffix, metrics in ratios.items()
        },
        "suite_normalized_throughput_ratio": round(throughput["guide"] / throughput["baseline"], 5)
        if throughput["guide"] is not None and throughput["baseline"]
        else None,
    }
    if matrix:
        result["grouped_ratio"] = {
            group: {
                value: {name: round(sum(items) / len(items), 4) for name, items in metrics.items()}
                for value, metrics in groups.items()
            }
            for group, groups in grouped.items()
        }
    return result


def matrix_comparison(guide_results: list[dict], baseline_results: list[dict]) -> dict | None:
    """Join successful points by ISL/OSL and stages by explicit concurrency."""
    pairs = []
    for guide, baseline in _matched(
        [point for point in guide_results if point.get("status") == "succeeded"],
        [point for point in baseline_results if point.get("status") == "succeeded"],
        ("isl", "osl"),
    ):
        for left, right in _matched(
            guide.get("stage_metrics") or [], baseline.get("stage_metrics") or [], ("concurrency",)
        ):
            pairs.append(
                (
                    {
                        "isl": guide["isl"],
                        "osl": guide["osl"],
                        "concurrency": left["concurrency"],
                        "num_requests": left.get("num_requests"),
                    },
                    left,
                    right,
                )
            )
    return _aggregate(pairs, matrix=True)


def rate_stage_comparison(guide_stages: list[dict], baseline_stages: list[dict]) -> dict | None:
    """Join rate stages by explicit target rate; order never implies parity."""
    return _aggregate(
        [
            ({"rate": guide["rate"], "duration": guide.get("duration")}, guide, baseline)
            for guide, baseline in _matched(guide_stages, baseline_stages, ("rate",))
        ]
    )
