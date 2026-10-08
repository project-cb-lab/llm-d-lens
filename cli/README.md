# CLI

The first package is [`rollouts`](rollouts/README.md): prepare Git-based v0/v1
snapshots, validate modelserver/router isolation, and deploy a candidate with a
functional-test Kubernetes Job. LiteLLM and traffic splitting are deferred.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e './cli[dev]'
.venv/bin/lens-rollouts --help
```

Run those commands from the Lens repository root. Python 3.11+, Git, Kustomize
5.8.1+, and Helm are needed; kubectl is also needed for cluster operations.
See the package README for the manifest contract and a complete rollout example.

```sh
cd cli
../.venv/bin/python -m pytest
ruff check rollouts tests
```
