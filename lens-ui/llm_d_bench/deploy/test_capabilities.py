from llm_d_bench.deploy.capabilities import (
    deployment_capabilities,
    provider_capability,
    provider_supports,
)


def test_capability_lookup_returns_copies() -> None:
    capability = provider_capability("precise-prefix-cache-routing")
    assert capability is not None
    assert capability["supports_kubernetes_service_baseline"] is True
    capability["supports_kubernetes_service_baseline"] = False
    assert provider_supports("precise-prefix-cache-routing", "supports_kubernetes_service_baseline")


def test_unknown_provider_and_capability_are_not_supported() -> None:
    assert provider_capability("unknown") is None
    assert not provider_supports("unknown", "supports_kubernetes_service_baseline")
    assert not provider_supports("optimized-baseline", "unknown_capability")
    assert provider_supports("optimized-baseline", "supports_kubernetes_service_baseline")


def test_deployment_capabilities_expose_provider_ids() -> None:
    capabilities = deployment_capabilities()
    assert len({item["id"] for item in capabilities}) == len(capabilities)
    assert all(item["supported"] is True for item in capabilities)


def test_evaluation_defaults_are_declarative_and_copy_safe() -> None:
    capability = provider_capability("tiered-prefix-cache")
    assert capability is not None
    assert capability["evaluation"]["default_variant"] == "native/cpu/base"
    assert capability["evaluation"]["required_baselines"] == ["direct-vllm"]
    unavailable = capability["evaluation"]["unavailable_variants"]
    assert {item["id"] for item in unavailable} == {
        "native/fs/base",
        "lmcache-connector/fs/base",
    }


def test_three_additional_guides_expose_complete_evaluation_plans() -> None:
    expected = {
        "optimized-baseline": (["kubernetes-service", "load-only", "affinity-only"], "shared-prefix"),
        "precise-prefix-cache-routing": (["kubernetes-service", "optimized-baseline"], "shared-prefix"),
        "pd-disaggregation": (["direct-vllm"], "matrix"),
    }
    for provider, (baselines, workload_kind) in expected.items():
        evaluation = provider_capability(provider)["evaluation"]
        assert evaluation["required_baselines"] == baselines
        assert evaluation["recommended_workload"]["kind"] == workload_kind
        assert evaluation["default_goal"] == "full-evaluation"
        assert evaluation["goals"]
        assert evaluation["comparison_arms"]
        assert evaluation["evidence"]
        assert evaluation["validation"]


def test_routing_workload_instances_do_not_share_nested_mutable_state():
    from llm_d_bench.deploy.capabilities import shared_prefix_routing_workload

    first, second = shared_prefix_routing_workload(), shared_prefix_routing_workload()
    first["stages"][0]["rate"] = -1
    assert second["stages"][0]["rate"] == 3
    optimized = provider_capability("optimized-baseline")["evaluation"]
    precise = provider_capability("precise-prefix-cache-routing")["evaluation"]
    assert optimized["recommended_workload"]["shared_prefix"] is not precise["recommended_workload"]["shared_prefix"]
    assert optimized["recommended_workload"]["shared_prefix"] is not optimized["goals"][0]["scenarios"][0]["benchmark"]["shared_prefix"]
