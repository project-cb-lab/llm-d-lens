import shutil

import pytest

from llmd.catalog import find_stack
from llmd.cli import main
from llmd.errors import CommandError
from llmd.process import subprocess_runner
from llmd.render import render
from llmd.sources import resolve_source

needs_kustomize = pytest.mark.skipif(
    shutil.which("kustomize") is None and shutil.which("kubectl") is None, reason="needs kustomize"
)


@needs_kustomize
def test_an_overlay_renders_like_oc_apply_k(cbrepo):
    stack = find_stack(resolve_source(str(cbrepo)), "manifests/production/model-a/cluster-a")
    rendered = render(stack, runner=subprocess_runner)
    kinds = {
        (doc["kind"], doc["metadata"]["name"], doc["metadata"].get("namespace")) for doc in rendered.documents
    }
    assert kinds == {("Deployment", "model-a", "model-a-serving"), ("Service", "model-a", "model-a-serving")}
    deployment = next(doc for doc in rendered.documents if doc["kind"] == "Deployment")
    assert deployment["spec"]["replicas"] == 1  # the overlay's own replicas
    assert "manifests/production/model-a/cluster-a" in rendered.command


def test_a_manifest_directory_is_read_as_it_is(cbrepo):
    stack = find_stack(resolve_source(str(cbrepo)), "manifests/experiments/model-c/plain")
    rendered = render(stack, runner=subprocess_runner)
    assert [doc["kind"] for doc in rendered.documents] == ["Deployment"]  # values files are not manifests


@needs_kustomize
def test_a_broken_kustomization_is_a_command_error(cbrepo):
    (cbrepo / "manifests/production/model-a/cluster-a/kustomization.yaml").write_text(
        "resources: [missing]\n"
    )
    stack = find_stack(resolve_source(str(cbrepo)), "manifests/production/model-a/cluster-a")
    with pytest.raises(CommandError, match="kustomize render of manifests/production/model-a/cluster-a"):
        render(stack, runner=subprocess_runner)


@needs_kustomize
def test_render_command(cbrepo, tmp_path, capsys):
    argv = ["--state-dir", str(tmp_path), "render", "manifests/production/model-a/cluster-a"]
    assert main([*argv, "--source", str(cbrepo)]) == 0
    captured = capsys.readouterr()
    assert "kind: Deployment" in captured.out and "2 object(s)" in captured.err
