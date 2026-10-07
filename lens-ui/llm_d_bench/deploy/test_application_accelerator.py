"""``_deploy_accelerator`` resolves a run's hardware from its own data, never an env var."""

from __future__ import annotations

from types import SimpleNamespace

from llm_d_bench.deploy.application import _deploy_accelerator


def _run(*, provenance=None, source_configurations=()):
    return SimpleNamespace(provenance=provenance or {}, source_configurations=list(source_configurations))


def test_returns_none_when_nothing_recorded_an_accelerator():
    assert _deploy_accelerator(None) is None
    assert _deploy_accelerator(_run()) is None


def test_reads_the_run_s_own_provenance_first():
    run = _run(provenance={"accelerator": "gpu"})
    assert _deploy_accelerator(run) == "gpu"


def test_reads_a_source_configuration_s_officialguide_source_accelerator():
    """Design Configuration records the UI's hardware pick nested under
    ``officialGuide.source.accelerator`` -- the actual shape produced by the
    Configuration module, not a flat top-level key."""
    configuration = SimpleNamespace(
        content={"officialGuide": {"source": {"accelerator": "gpu"}}},
        provenance={},
    )
    run = _run(source_configurations=[configuration])
    assert _deploy_accelerator(run) == "gpu"


def test_reads_a_comparison_draft_s_flat_accelerator_key():
    """The Evaluation comparison-draft flow strips officialGuide but preserves
    a flat ``accelerator`` key (see comparisonConfigurationDraft on the frontend)."""
    configuration = SimpleNamespace(content={"accelerator": "gpu"}, provenance={})
    run = _run(source_configurations=[configuration])
    assert _deploy_accelerator(run) == "gpu"


def test_run_level_provenance_wins_over_a_source_configuration_value():
    configuration = SimpleNamespace(content={"accelerator": "xpu"}, provenance={})
    run = _run(provenance={"accelerator": "gpu"}, source_configurations=[configuration])
    assert _deploy_accelerator(run) == "gpu"
