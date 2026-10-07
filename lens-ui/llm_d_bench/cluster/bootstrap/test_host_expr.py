"""Unit tests for host-spec expansion (design §3.1: single IP / comma list /
IP range)."""

from __future__ import annotations

import pytest

from llm_d_bench.cluster.bootstrap.host_expr import expand_host_spec


def test_single_host():
    assert expand_host_spec("10.0.0.5") == ["10.0.0.5"]


def test_single_hostname_passes_through():
    assert expand_host_spec("worker-1.internal") == ["worker-1.internal"]


def test_comma_separated_list():
    assert expand_host_spec("10.0.0.5, 10.0.0.6,10.0.0.9") == ["10.0.0.5", "10.0.0.6", "10.0.0.9"]


def test_comma_separated_list_dedupes_preserving_order():
    assert expand_host_spec("10.0.0.5, 10.0.0.6, 10.0.0.5") == ["10.0.0.5", "10.0.0.6"]


def test_shorthand_range():
    assert expand_host_spec("10.0.0.10-13") == ["10.0.0.10", "10.0.0.11", "10.0.0.12", "10.0.0.13"]


def test_shorthand_range_rejects_start_after_end():
    with pytest.raises(ValueError):
        expand_host_spec("10.0.0.20-10")


def test_full_range_spanning_octet_boundary():
    assert expand_host_spec("10.0.0.254-10.0.1.1") == ["10.0.0.254", "10.0.0.255", "10.0.1.0", "10.0.1.1"]


def test_full_range_rejects_start_after_end():
    with pytest.raises(ValueError):
        expand_host_spec("10.0.1.1-10.0.0.254")


def test_mixed_list_of_ranges_and_singles():
    assert expand_host_spec("10.0.0.1, 10.0.0.10-12") == [
        "10.0.0.1",
        "10.0.0.10",
        "10.0.0.11",
        "10.0.0.12",
    ]


def test_blank_spec_is_rejected():
    with pytest.raises(ValueError):
        expand_host_spec("   ")


def test_malformed_range_shaped_token_falls_back_to_literal_host():
    # Doesn't match either recognized range shape, so it's treated as an
    # ordinary (if unusual) hostname rather than a parse error -- avoids
    # false positives against real hostnames containing dashes.
    assert expand_host_spec("not-a-valid-range-1-2-3") == ["not-a-valid-range-1-2-3"]


def test_shorthand_range_rejects_out_of_bounds_octet():
    with pytest.raises(ValueError):
        expand_host_spec("10.0.0.5-999")


def test_large_shorthand_range_expands_fully():
    hosts = expand_host_spec("10.0.0.1-200")
    assert len(hosts) == 200
    assert hosts[0] == "10.0.0.1"
    assert hosts[-1] == "10.0.0.200"
