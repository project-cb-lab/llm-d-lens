import copy
import shutil
from pathlib import Path

import pytest

from rollouts.common import run, write
from rollouts.prepare import prepare

FIXTURES = Path(__file__).parent / "data"


@pytest.fixture
def repo(tmp_path):
    if not (shutil.which("kustomize") or shutil.which("kubectl")):
        pytest.skip("requires kustomize or kubectl")
    root = tmp_path / "manifests"
    root.mkdir()
    run(["git", "init", "-b", "main"], cwd=root)
    run(["git", "config", "user.name", "Rollout tests"], cwd=root)
    run(["git", "config", "user.email", "tests@example.invalid"], cwd=root)
    run(["git", "config", "commit.gpgsign", "false"], cwd=root)
    source = root / "deployments/model/practice"
    source.mkdir(parents=True)
    write(
        source / "kustomization.yaml",
        {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": ["model-server/disaggregatedset.yaml"],
        },
    )
    roles = []
    for name in ("prefill", "decode"):
        template = {
            "metadata": {"labels": {"app": "model", "role": name}},
            "spec": {"containers": [{"name": "server", "image": "example.invalid/model:v1"}]},
        }
        roles.append(
            {
                "name": name,
                "spec": {
                    "replicas": 1,
                    "leaderWorkerTemplate": {
                        "size": 2,
                        "workerTemplate": template,
                        "leaderTemplate": copy.deepcopy(template),
                    },
                },
            }
        )
    write(
        source / "model-server/disaggregatedset.yaml",
        {
            "apiVersion": "disaggregatedset.x-k8s.io/v1",
            "kind": "DisaggregatedSet",
            "metadata": {"name": "model", "namespace": "practice"},
            "spec": {"slices": 1, "roles": roles},
        },
    )
    write(
        source / "router/router.values.yaml",
        {
            "router": {
                "modelServers": {"matchLabels": {"app": "model"}},
                "inferenceObjectives": [{"name": "premium", "priority": 100}],
                "epp": {"pluginsCustomConfig": {"plugins.yaml": "kind: EndpointPickerConfig\n# a comment\n"}},
                "proxy": {
                    "presets": {
                        "envoy": {
                            "configMap": {"name": "envoy-fixed"},
                            "volumes": [{"name": "config", "configMap": {"name": "envoy-fixed"}}],
                        }
                    }
                },
            },
        },
    )
    write(
        source / "rollout-config.yaml",
        {
            "schema": 1,
            "router": {
                "chart": {"name": "router", "repo": "oci://example.invalid/charts", "version": "1.0.0"},
                "values": ["router/router.values.yaml"],
            },
        },
    )
    commit(root, "baseline")
    return root


def commit(repo, message):
    run(["git", "add", "."], cwd=repo)
    run(["git", "commit", "-m", message], cwd=repo)
    return run(["git", "rev-parse", "HEAD"], cwd=repo).strip()


@pytest.fixture
def prepared(repo, tmp_path):
    if not shutil.which("helm"):
        pytest.skip("requires Helm")
    before = run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    values = repo / "deployments/model/practice/router/router.values.yaml"
    values.write_text(values.read_text().replace("priority: 100", "priority: 90"))
    after = commit(repo, "router change")
    root, state = prepare(
        repo,
        from_ref=before,
        to_ref=after,
        model="model",
        environment="practice",
        output=tmp_path / "rollout",
    )
    for slot in ("v0", "v1"):
        shutil.copytree(FIXTURES / "router", root / slot / "charts/router-1.0.0/router")
    return root, state
