## About

**Lens** is an open-source infrastructure for operating the life cycle of large language models on
[llm-d](https://llm-d.ai/), the Kubernetes-native distributed inference stack.

Serving an LLM in production usually means stitching together a handful of unrelated tools: one to
plan capacity, another to roll out, another to benchmark, another to watch it. Lens puts all of that
behind a single interface exposed through a `cli` or through a `visual web-based control-plane`, so
a team can go from a registered model to a benchmarked, production-serving deployment without
leaving one place.

## Components

This repository is a monorepo with two components that share a single backend contract.

| Component | Path | What it does |
| --- | --- | --- |
| **CLI** | [`cli/`](cli) | Drives deployments and roll outs from the CLI, and exposes the backend that the UI is built on. Scriptable and CI-friendly. |
| **Lens UI** | [`lens-ui/`](lens-ui) | A single pane of glass over your inference infrastructure — everything the CLI can do, visually. |

> [!NOTE]
> The CLI is under active development and its reference docs are still being written.
> See [`cli/README.md`](cli/README.md) for current status.

## Getting Started

The fastest path to a running instance is the bundled Ubuntu installer, which provisions Node,
Python, an embedded PostgreSQL database, TLS and the application itself:

```bash
git clone https://github.com/llm-d-incubation/llm-d-lens.git
```
