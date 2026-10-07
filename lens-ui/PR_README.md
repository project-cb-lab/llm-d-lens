# PR: Remove Original Prism Features and Retain Lens Workflows

## Purpose

The application extends llm-d Prism. The original demo dashboards, result imports,
and review features are outside the current product scope. This PR removes their
pages, APIs, dedicated tools, and documentation while retaining Lens features and
shared dependencies. Old page links fall back to Model market; removed APIs return
JSON 404 responses.

## Retained features

| Page | Route |
| --- | --- |
| Model market | `model-market` |
| Deployments | `optimization-deployments` |
| Evaluation | `optimization-evaluate` |
| Simulation | `optimization-simulate` |
| Lens Assistant | `playground` |
| Clusters | `clusters` |
| Storage | `storage-management` |
| Model cache | `model-cache` |
| External providers | `ai-providers` |
| Cluster monitoring | `cluster-monitoring-stack` |

Evaluation creation/details, configuration editing, candidate search, and deployment
planning remain. The Python backend, MCP, Docker/Compose, development scripts, and
shared components required by these pages also remain.

## Main changes

### 1. Remove original pages and dedicated frontend code

- Remove the Prism home page and Intelligent routing, Prefix cache offloading,
  Agentic serving, and PD disaggregation demo dashboards.
- Remove Results Store, Workload catalog, Regressions & analysis, Benchmark browser,
  and Schema explorer.
- Remove their data-source hooks, result parsers, charts, filters, sharing encoders,
  and archived data.
- Clean up App routes, the sidebar, GitHub login Provider, and Vite proxy configuration.
- Remove unused shared components and legacy chart helpers.
- Remove ModelCacheTable, BenchmarkPlanEditor, benchmarkSuites, evaluation adapters,
  and their dedicated tests, which no longer belong to any current page dependency chain.

### 2. Remove legacy APIs, authentication, and imports

- Remove the Results API, GitHub OAuth/IAM, and GCS/GIQ/Drive data-access logic.
- Remove obsolete collection, import, processing, and access-management scripts
  and their MCP tools.
- Remove unused direct npm dependencies and update the lockfile.
- Return JSON 404 responses for unknown or removed `/api` requests instead of frontend HTML.

### 3. Clean up build, release, and development configuration

- Remove the original site's Cloud Run release scripts, workflows, and production
  configuration; retain Lens cluster deployment and container execution.
- Remove upstream-specific review/auto-merge/release workflows, maintainer lists,
  sign-off instructions, and security-reporting guidance. Update contribution
  guidance and issue/PR templates for this repository.
- Remove obsolete GIQ nginx configuration, Cloud Run ignore files, old issue-fix
  guidance, and empty directories; retain the original favicon.
- Default local image builds to `lens`; remove the upstream image-push target.
- Remove the Go placeholder service and Go-only checks.
- Use Node/Python build, test, and lint targets in Makefile; add frontend build,
  MCP generation, type checks, and Node tests to CI.
- Clean up environment examples, obsolete Compose settings, and MCP generator comments.

### 4. Update documentation and tests

- Update README with Lens features and startup instructions; set the page title to Lens.
- Remove original feature specifications, role descriptions, screenshots, and obsolete
  development skills; update contribution and style guidance still in use.
- Correct workspace messages that refer to removed Results Store/Workload Catalog features.
- Update stale assertions in three evaluation tests to match existing 8 GiB memory
  and three-stage throughput presets; evaluation runtime behavior is unchanged.
- Fix the AI Providers AsyncState import and missing closing tag; add a type guard
  for regex match positions in Playground text parsing.
- Send the workspace completion button to Evaluation instead of Results Store.
- Retain the Apache 2.0 LICENSE and source copyright notices.

## Verification

| Check | Result |
| --- | --- |
| `npm run build` | Passed; existing bundle-size warning remains |
| `npm run type-check` | Passed |
| `make test-js` | 282 passed |
| `make test-python` | 1008 passed, 4 pre-existing failures |
| Initial rendering of 10 pages | Passed |
| Frontend entry dependencies | 170 reachable source modules; no broken local imports |
| Old pages and APIs | 9 old page links fall back; 9 old APIs return 404 |
| MCP tool catalog consistency | Passed |
| Development services | Frontend, Node, and Python respond; cluster and provider APIs return 200 through the frontend proxy |

The four Python failures were reproduced on the Git baseline before cleanup. They
involve deployment smoke-test commands, cluster capacity validation, and two legacy
shared-prefix cases. See the [cleanup record](docs/archive/prism-cleanup.md) for names
and reproduction details.

Real cluster deployment, external model calls, browser interaction end-to-end tests,
and Docker image builds were not performed.

## Usage notes

- Original Prism Results Store, GitHub contribution authentication, and GCS/GIQ/Drive
  imports are no longer supported.
- Generate the MCP tool catalog before the first local run; see [README](README.md).
- `scripts/dev.sh` defaults to HTTPS. If ports are occupied, it selects available ports
  and updates proxy settings; use the addresses in the actual startup log.
