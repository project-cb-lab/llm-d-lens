import pytest

from llmd.catalog import default_name, find_stack, list_stacks
from llmd.cli import main
from llmd.errors import NotFoundError, UsageError
from llmd.process import subprocess_runner
from llmd.sources import resolve_source


def test_overlays_are_stacks_and_their_bases_are_not(cbrepo):
    source = resolve_source(str(cbrepo))
    assert [stack.id for stack in list_stacks(source, subprocess_runner)] == [
        "manifests/production/model-a/cluster-a"
    ]


def test_listing_by_prefix(cbrepo):
    source = resolve_source(str(cbrepo))
    assert list_stacks(source, subprocess_runner, "manifests/experiments") == []
    assert len(list_stacks(source, subprocess_runner, "manifests/production/")) == 1


def test_a_stack_is_any_path_with_manifests(cbrepo):
    source = resolve_source(str(cbrepo))
    assert find_stack(source, "manifests/production/model-a/cluster-a").kind == "kustomize"
    plain = find_stack(source, "manifests/experiments/model-c/plain/")
    assert (plain.id, plain.kind) == ("manifests/experiments/model-c/plain", "manifests")
    single = find_stack(source, "manifests/experiments/model-c/plain/deployment.yaml")
    assert single.kind == "manifests"
    with pytest.raises(NotFoundError):
        find_stack(source, "manifests/production/missing")
    with pytest.raises(UsageError, match="escapes"):
        find_stack(source, "../outside")
    with pytest.raises(UsageError, match="no Kubernetes manifests"):
        find_stack(source, "charts/router/Chart.yaml")


def test_default_names_are_short_dns_labels(cbrepo):
    source = resolve_source(str(cbrepo))
    assert default_name(find_stack(source, "manifests/production/model-a/cluster-a")) == "model-a-cluster-a"
    long = find_stack(source, "manifests/experiments/model-c/plain")
    long = type(long)(id="manifests/" + "x" * 60 + "/cluster.v1.2", kind="kustomize", source=source)
    name = default_name(long)
    assert len(name) <= 40 and name.endswith(name[-8:]) and name.islower()


def test_stacks_command(cbrepo, tmp_path, capsys):
    assert main(["--state-dir", str(tmp_path / "s"), "stacks", "--source", str(cbrepo)]) == 0
    out = capsys.readouterr().out
    assert "1 stack(s)" in out and "manifests/production/model-a/cluster-a" in out
