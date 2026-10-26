# `llmd`

`llmd` plans, stands up, rolls out, and removes [curvebender-tools](https://github.com/project-cb-lab/curvebender-tools)
stacks inside one existing Kubernetes or OpenShift namespace. It never needs cluster-admin rights.

The CLI is being built up in small steps; this file grows with it.

## Install

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -e ./cli
.venv/bin/llmd --version
```

## Development

```sh
cd cli
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/ruff check llmd tests
.venv/bin/ruff format --check llmd tests
```
