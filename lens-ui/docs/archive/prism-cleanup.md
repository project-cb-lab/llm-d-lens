# Original Prism Feature Cleanup Record

Comparison directory: `/home/wenjiao/offical/llm-d-prism` (read-only).
Working directory: `/home/wenjiao/llm-d-prism`.

## Retained scope

Retain model-market, optimization-deployments, optimization-evaluate,
optimization-simulate, playground, clusters, storage-management, model-cache,
ai-providers, cluster-monitoring-stack, and their subflows: evaluation creation/details,
configuration editing, candidate search, deployment planning, and workspaces.

Retain the Python backend, MCP/Assistant, cluster/storage management, model cache,
monitoring, evaluation charts, and original shared UI, error boundaries, and utilities
still used by these pages. Originating in Prism alone is not grounds for removal.
Retain LICENSE, copyright notices, runtime data, and local configuration; the original
directory was not modified.

## Removed or updated

- Original home page and Intelligent routing, Prefix cache offloading, Agentic serving,
  and PD disaggregation demonstration pages.
- Results Store, Workload catalog, Regressions & analysis, Benchmark browser, Schema explorer,
  and dedicated charts, filters, parsers, data-source hooks, and sharing encoders.
- Legacy Results APIs, GitHub OAuth/IAM, GCS/GIQ/Drive data access/import scripts,
  archived data, associated MCP tools, and direct dependencies.
- Unused FactCell, StatPills, legacy axes, range helpers, and theme helpers.
- Original Cloud Run release scripts, production configuration, and OAuth/release
  guidance. Lens Dockerfile, Compose, development scripts, and cluster deployment remain.
- Go placeholder service and Go lint/Dependabot settings; Makefile/CI now check Node/Python.
- Original feature specifications, old user-role descriptions, old benchmark-analysis
  skills, and home-page screenshots; retain Lens specifications.
- README, styling/development guidance, environment examples, page titles, and legacy references.

This continued cleanup edits already present before the task. The workspace was not
reset, committed, or published during that cleanup.

## Verification results

- `npm run build`: passed (existing large-chunk warnings remain).
- `npm run type-check`: passed.
- `make test-js`: 287/287 passed.
- `make test-python`: 1008 passed, 4 failed. The four failures were reproduced in a
  temporary directory using Git HEAD snapshots of `llm_d_bench`, `tests`, and
  `pyproject.toml`, confirming pre-existing issues.
- All 10 retained pages passed initial server rendering through the App entry point.
- Checked 564 frontend/backend local import/export references; no broken paths.
- A separate temporary Node service served the production SPA and configuration API;
  MCP GET returned the expected 405. Old Results, local data/submission, prefix-cache,
  PD, regressions, and GitHub-login endpoints returned 404.
- Regenerated the MCP catalog and passed `--check`; package.json and lock dependencies match.
- Modified YAML parsed successfully; `git diff --check` passed.

Three Node tests had stale assertions: commit `994aba6` changed default memory from
32 GiB to 8 GiB and throughput presets from four stages to three without updating the
tests. Only expectations were updated; evaluation behavior was unchanged.

Pre-existing Python failures:

1. `deploy/runtime/test_composition.py::test_published_deployment_names_scope_commands_and_smoke_test`
2. `evaluate/test_deployments.py::test_evaluation_capacity_allows_waiting_for_cards`
3. `evaluate/test_shared_prefix.py::test_shared_prefix_workload_yaml_is_open_loop_with_reused_system_prompts[legacy_settings0]`
4. The same test with `[legacy_settings1]`.

Paths above are relative to `llm_d_bench/`. No real cluster deployments, external model
calls, browser interaction end-to-end tests, or Docker image builds were performed.
Initial rendering cannot substitute for those checks.

## Second completeness review

Traversing static imports, re-exports, and literal dynamic imports from `src/main.jsx`
identified four additional unused historical Lens modules: `ModelCacheTable`,
`BenchmarkPlanEditor`, `benchmarkSuites`, and `features/evaluation/adapters`. They and
two dedicated test files were removed. They were not original Prism features but no
longer served current pages.

Historical candidates in the workspace are local mocks and do not access Results Store.
Their descriptions now explicitly identify examples, and promises concerning the removed
Workload Catalog were removed. Cleanup records and planning directories are no longer
Git-ignored, allowing future commits to include them.

Final recheck: build and type checks passed; 282/282 Node tests passed (five obsolete-module
cases removed); Python remained at 1008 passed with the same four pre-existing failures.
All 170 frontend source modules were reachable from the application entry through imports,
with no orphan modules or broken local imports. Ten pages passed initial rendering;
nine old page links fell back to Model market; nine old APIs returned JSON 404; MCP
catalog consistency passed. These checks establish cleanup scope, not real-cluster or
browser end-to-end acceptance.

## Upstream configuration and empty-directory cleanup (2026-09-18)

At the user's request, retain the old brand icons and remove upstream files that do
not apply to Lens:

- Remove sign-off checks, branch gates, Prow review/auto-merge, and stale/unstale
  workflows dependent on `llm-d-infra`.
- Remove the release workflow publishing to `ghcr.io/llm-d`, its two exclusive
  composite actions, and the old Copilot setup workflow installing gh-aw.
- Remove upstream `OWNERS`/`CODEOWNERS`, Prow labels, sign-off instructions, and
  security-reporting documents. Upstream contacts and response times do not describe Lens maintenance.
- Remove unused `.gcloudignore`, the old GIQ proxy `nginx.conf`, and issue-fix guidance
  requiring branches from `prism/main`.
- Update contribution guidance and issue/PR templates for this repository; remove
  broken links and upstream-only rules. Retain Code of Conduct, LICENSE, copyright
  notices, current PR CI, Dependabot, and local check configuration.
- Remove obsolete Go, Results Store, and deleted-skill entries from ignore configuration.
- Retain local image builds in Makefile, default the image name to `lens`, and remove
  upstream image-push targets. Dockerfile, Compose, model-cache downloaders, and llm-d
  image dependencies used for inference deployment remain unchanged.
- Remove 29 empty directories, including empty parents, `public/data`, `dist/data`,
  old frontend directories, Go placeholders, and old role/specification directories.
  Do not clean dependency environments, Git, caches, or private runtime data.

The three favicons in `public` and all files in `dist` remain. The ten current pages
and backend business source were unchanged.

Validation for this round: `npm run build` passed with the existing bundle warning;
12 remaining YAML files parsed; Docker/Podman's `make -n image-build` generated valid
commands; updated local documentation links resolved; no references to removed
configuration remained outside historical records; `git diff --check` passed.
SHA-256 comparison of 513 files under `src`, `server`, `llm_d_bench`, `public`, and
`dist` confirmed identical file sets and contents before and after cleanup. No empty
directories remained in scope, and LICENSE was unchanged. Full business tests,
GitHub-hosted workflows, and actual container builds were not run.
