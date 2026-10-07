"""Regression tests for `_readiness_is_transient`.

A brief inability to reach the cluster's control plane (connection refused,
DNS/dial errors, timeouts, ...) says nothing about whether the workload itself
is healthy. These reasons must be classified as transient so a momentary
network blip doesn't permanently flip a healthy deployment to `failed`.
"""

import pytest

from llm_d_bench.deploy.service import _readiness_is_transient


@pytest.mark.parametrize(
    "reason",
    [
        "The connection to the server 10.112.110.140:33481 was refused - did you specify the right host or port?",
        "dial tcp 10.0.0.1:6443: connect: connection refused",
        "Unable to connect to the server: net/http: TLS handshake timeout",
        "Unable to connect to the server: dial tcp: lookup api.cluster: no such host",
        "read: connection reset by peer",
        "no route to host",
        "context deadline exceeded",
        "kubectl command timed out",
        "helm command timed out",
        "endpoint smoke test timed out",
        'Error from server (NotFound): deployments.apps "optimized-baseline-xpu-vllm-decode" not found',
    ],
)
def test_connectivity_and_timeout_reasons_are_transient(reason):
    assert _readiness_is_transient([reason]) is True


@pytest.mark.parametrize(
    "reason",
    [
        "pod modelserver-0 is in CrashLoopBackOff",
        "ImagePullBackOff: repository does not exist",
        'Error from server (NotFound): secrets "llm-d-hf-token" not found',
    ],
)
def test_workload_failure_reasons_are_not_transient(reason):
    assert _readiness_is_transient([reason]) is False


def test_empty_reasons_are_not_transient():
    assert _readiness_is_transient([]) is False


def test_resource_shortage_waits_but_does_not_mask_crashed_workload():
    assert _readiness_is_transient(["0/1 nodes are available: Insufficient nvidia.com/gpu"])
    assert _readiness_is_transient(["waiting for ResourceClaim allocation"])
    assert not _readiness_is_transient(["Insufficient nvidia.com/gpu", "CrashLoopBackOff"])


def test_rollout_deadline_with_pending_resource_claim_keeps_waiting():
    assert _readiness_is_transient(
        ["deployment exceeded progress deadline"],
        {
            "pods": "model-0 0/1 Pending",
            "events": "FailedScheduling Insufficient nvidia.com/gpu",
        },
    )
    assert not _readiness_is_transient(
        ["deployment exceeded progress deadline"],
        {
            "pods": "model-0 0/1 CrashLoopBackOff",
            "events": "FailedScheduling Insufficient nvidia.com/gpu",
        },
    )
