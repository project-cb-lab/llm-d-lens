"""Comparisons must join measurements by load, never by array position."""

import importlib

import pytest

router = importlib.import_module("llm_d_bench.evaluate.router")


def stage(load, value=10, **extra):
    return {
        **load,
        "metrics": {"throughput_tps": value, "total_output_tokens": value * 10, "benchmark_time_seconds": 10},
        **extra,
    }


def point(stages, **extra):
    return {"isl": 1024, "osl": 128, "status": "succeeded", "stage_metrics": stages, **extra}


def test_rate_mismatch_is_not_compared_by_position():
    assert router._rate_stage_comparison([stage({"rate": 1})], [stage({"rate": 10})]) is None


def test_concurrency_mismatch_is_not_compared_by_position():
    assert router._matrix_comparison([point([stage({"concurrency": 1})])], [point([stage({"concurrency": 8})])]) is None


def test_reordered_rates_match_by_value():
    report = router._rate_stage_comparison(
        [stage({"rate": 1}, 20), stage({"rate": 10}, 60)], [stage({"rate": 10}, 30), stage({"rate": 1}, 10)]
    )
    assert [row["ratio"]["throughput_tps"] for row in report["rows"]] == [2, 2]


@pytest.mark.parametrize("load", [{}, {"rate": None}])
def test_unknown_rates_do_not_establish_parity(load):
    assert router._rate_stage_comparison([stage(load)], [stage(load)]) is None


def test_ambiguous_duplicate_rates_are_not_arbitrarily_selected():
    assert router._rate_stage_comparison([stage({"rate": 1})], [stage({"rate": 1}, 10), stage({"rate": 1}, 30)]) is None


def test_zero_is_a_measurement_not_missing():
    report = router._rate_stage_comparison([stage({"rate": 1}, 0)], [stage({"rate": 1}, 10)])
    assert report["rows"][0]["ratio"]["throughput_tps"] == 0
    assert report["geometric_mean_ratio"]["throughput_tps"] == 0
    assert report["suite_normalized_throughput_ratio"] == 0


def test_zero_denominator_and_missing_metrics_have_no_ratio():
    report = router._rate_stage_comparison([stage({"rate": 1}, 10)], [stage({"rate": 1}, 0)])
    assert "throughput_tps" not in report["rows"][0]["ratio"]
    assert "ttft_ms" not in report["rows"][0]["ratio"]


def test_suite_throughput_uses_only_pairs_with_both_durations():
    guide = [stage({"rate": 1}, 20), stage({"rate": 2}, 100)]
    baseline = [stage({"rate": 1}, 10), {"rate": 2, "metrics": {"throughput_tps": 10}}]
    assert router._rate_stage_comparison(guide, baseline)["suite_normalized_throughput_ratio"] == 2
