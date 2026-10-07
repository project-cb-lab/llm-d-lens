<!-- markdownlint-disable MD001 MD041 -->
<p align="center">
  <img alt="Lens" src="public/lens_logo.png" width="150">
</p>

<h3 align="center">
An open-source control plane for AI inference on llm-d
</h3>

<p align="center">
| <a href="https://llm-d-lens.docs.buildwithfern.com/"><b>Documentation</b></a> | <a href="https://llm-d-lens.docs.buildwithfern.com/quickstart"><b>Quickstart</b></a> | <a href="https://llm-d-lens.docs.buildwithfern.com/user-guide/model-market"><b>User Guide</b></a> | <a href="https://llm-d-lens.docs.buildwithfern.com/architecture"><b>Architecture</b></a> |
</p>

---

## About

Lens is an open-source control plane for deploying and operating large language models on [llm-d](https://llm-d.ai/), the Kubernetes-native distributed inference stack, across your Kubernetes clusters. It plans and executes inference deployments, runs guided evaluations and traffic simulations, and keeps clusters, storage and external providers under one roof, so a team can go from a registered model to a benchmarked, production-serving deployment without stitching together separate tools.

Lens is built on llm-d with:

- Prefill/decode disaggregation, KV-cache-aware routing, KV cache offloading to CPU/SSD tiers, and latency-predicted routing that raise throughput and cut tail latency on the same accelerators
- Multi-cluster planning, approval and execution with agentic, workload-guided placement
- A multi-protocol model service gateway (OpenAI chat completions and embeddings, Anthropic messages) behind HTTPRoute → InferencePool → EPP, with per-model selection policies
- Role-based access control, per-cluster scoping and audit logging across every page
- A bootstrap-and-maintain monitoring stack (Prometheus/Grafana) for accelerator utilization and deployment health

Lens manages these areas from one console:

- **Model market** — browse models and start a one-click deployment
- **Deployments** — plan, approve and run inference deployments across clusters
- **Evaluation** — design benchmark experiments, run them against live deployments, and analyze or export the results
- **Simulation** — replay registered request traces against an endpoint to validate behavior and capacity
- **Model services** — publish cluster deployments as gateway-served model endpoints (OpenAI chat completions and embeddings, Anthropic messages)
- **Clusters** — register, bootstrap and monitor Kubernetes clusters
- **Storage** — manage volumes and file storage
- **Model cache** — share model weights across deployments
- **External providers** — connect external inference providers
- **Observability** — run the cluster monitoring stack
- **Lens Assistant** — an in-product assistant over the Lens API
- **Administration** — manage users, groups, roles, identity providers and API keys

## Getting Started

The fastest path to a running Lens instance is the bundled Ubuntu installer. It provisions Node, Python, an embedded PostgreSQL database, TLS and the application, so no manual setup is required:

```bash
git clone https://github.com/llm-d-incubation/llm-d-lens.git
cd llm-d-lens
./scripts/LensInstaller-Ubuntu-x86_64.sh
```

Press Enter to accept the defaults (install directory `$HOME/llm-d-lens`, app port `3000`, backend port `8081`). When it finishes, open the printed URL (`https://localhost:3000` by default) and sign in with the admin credentials it printed.

To work on the code instead of an installed deployment:

```bash
npm install
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev]'
node scripts/generate-mcp-tools.mjs
npm run dev
```

The frontend runs on port `5173`, the Node API on `3000`, and the Python backend is started by `scripts/run-backend.sh`. Use `npm run dev:web` when running the Python backend separately. `scripts/dev.sh` starts, stops and restarts the whole stack and regenerates the MCP tool catalog. See the [CLI Reference](https://llm-d-lens.docs.buildwithfern.com/cli-reference) for every subcommand.

Build and test:

```bash
npm run build
npm run type-check
make test-js
make test-python
```

Visit the [documentation](https://llm-d-lens.docs.buildwithfern.com/) to learn more:

- [Installation](https://llm-d-lens.docs.buildwithfern.com/installation)
- [Quickstart](https://llm-d-lens.docs.buildwithfern.com/quickstart)
- [User Guide](https://llm-d-lens.docs.buildwithfern.com/user-guide/model-market)

## Contributing

We welcome and value any contributions. Read [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md) before opening an issue or pull request.

Before changing behavior, follow [AGENTS.md](AGENTS.md) and the [reuse-first workflow](docs/refactoring/reuse-first-agent-design.md): find existing implementations, compare contracts and callers, and record reuse or new-code rationale in the PR. All repository text and PR titles and descriptions must be English; run `npm run check:english` before committing.

## License

Lens is released under the [Apache 2.0 license](LICENSE). The project retains the upstream Apache 2.0 license and copyright notices.

## Contact Us

- For bugs and feature requests, use GitHub [Issues](https://github.com/llm-d-incubation/llm-d-lens/issues).
- For security reports, use GitHub's [Security Advisories](https://github.com/llm-d-incubation/llm-d-lens/security/advisories).
