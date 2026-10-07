"""Deployments without an explicit model_secret should reuse the cluster's
wizard-created HF_TOKEN secret (see llm_d_bench.cluster.service.create_hf_token_secret
and CreateClusterWizard.jsx's mandatory HF_TOKEN secret step)."""

from llm_d_bench.cluster import registry
from llm_d_bench.deploy.service import _with_default_model_secret


def _cluster_with_recorded_secret():
    cluster = registry.create_cluster("c1", "desc", "kubeconfig: {}")
    return registry.update_cluster(
        cluster.id,
        hf_token_secret_namespace="llm-d-bench-storage-c1",  # noqa: S106 -- k8s namespace, not a credential
        hf_token_secret_name="hf-token-abc",  # noqa: S106 -- k8s Secret name, not a real credential
    )


def test_defaults_to_cluster_recorded_secret_when_none_configured():
    cluster = _cluster_with_recorded_secret()

    result = _with_default_model_secret({"guide_name": "optimized-baseline"}, None, cluster.id)

    assert result["model_secret"] == {
        "mode": "existing-secret",
        "sourceNamespace": "llm-d-bench-storage-c1",
        "sourceName": "hf-token-abc",
    }


def test_none_mode_falls_back_to_cluster_recorded_secret():
    cluster = _cluster_with_recorded_secret()
    explicit_none = {"mode": "none", "sourceNamespace": None, "sourceName": None}

    result = _with_default_model_secret({"model_secret": explicit_none}, None, cluster.id)

    assert result["model_secret"] == {
        "mode": "existing-secret",
        "sourceNamespace": "llm-d-bench-storage-c1",
        "sourceName": "hf-token-abc",
    }


def test_leaves_explicit_model_secret_untouched():
    cluster = _cluster_with_recorded_secret()
    explicit = {"mode": "host"}

    result = _with_default_model_secret({"model_secret": explicit}, None, cluster.id)

    assert result["model_secret"] == explicit


def test_leaves_explicit_existing_secret_source_untouched():
    cluster = _cluster_with_recorded_secret()
    explicit = {
        "mode": "existing-secret",
        "sourceNamespace": "other-ns",
        "sourceName": "other-secret",
    }

    result = _with_default_model_secret({"model_secret": explicit}, None, cluster.id)

    assert result["model_secret"] == explicit


def test_leaves_policy_untouched_when_a_per_request_token_is_supplied():
    cluster = _cluster_with_recorded_secret()

    result = _with_default_model_secret({}, "hf_supplied_token", cluster.id)  # noqa: S106

    assert "model_secret" not in result


def test_no_default_when_cluster_has_no_recorded_secret():
    cluster = registry.create_cluster("c1", "desc", "kubeconfig: {}")

    result = _with_default_model_secret({}, None, cluster.id)

    assert "model_secret" not in result


def test_no_default_when_cluster_id_is_missing_or_unknown():
    assert _with_default_model_secret({}, None, None) == {}
    assert _with_default_model_secret({}, None, "unknown-cluster-id") == {}


def test_tolerates_non_dict_deployment_policy():
    assert _with_default_model_secret(None, None, "unknown-cluster-id") is None
