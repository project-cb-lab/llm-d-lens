"""Tests for reading llm-d EPP token counters from Prometheus results."""

from llm_d_bench.model_service.usage_metrics import parse_token_query_results


def _series(value, **labels):
    metric = {"model_name": "qwen", "fairness_id": "mat-1", **labels}
    return {"metric": metric, "value": [1790388361.0, str(value)]}


def test_merges_counter_fields_and_sums_series_per_identity():
    usage = parse_token_query_results(
        {
            "input_tokens": [_series(100), _series(20, priority="9")],
            "output_tokens": [_series(45)],
            "cached_input_tokens": [_series(10)],
            "requests": [_series(3)],
        }
    )
    row = usage[("qwen", "mat-1")]
    assert row.input_tokens == 120
    assert row.output_tokens == 45
    assert row.cached_input_tokens == 10
    assert row.requests == 3


def test_missing_labels_default_and_unknown_fields_ignored():
    usage = parse_token_query_results(
        {
            "input_tokens": [{"metric": {"model_name": "m"}, "value": [0, "7"]}],
            "not_a_field": [_series(99)],
        }
    )
    assert usage[("m", "default-flow")].input_tokens == 7
    assert usage[("m", "default-flow")].output_tokens == 0


def test_malformed_value_is_zero():
    usage = parse_token_query_results(
        {
            "input_tokens": [{"metric": {"model_name": "m", "fairness_id": "t"}, "value": []}],
        }
    )
    assert usage[("m", "t")].input_tokens == 0
