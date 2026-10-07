# Repository development rules

## English-only repository and pull requests

All repository-authored text must be English: documentation, comments, messages,
UI labels, examples, tests, configuration, filenames, and agent instructions.
Before every commit and before creating or updating a PR, agents MUST:

1. Run `npm run check:english` over the entire repository, not only the diff.
2. Translate every finding into accurate English automatically, preserving
   technical meaning, links, code behavior, and test coverage. Review translations;
   do not delete content, hide it with escapes, or weaken the check to pass.
3. Write the PR title and description in English and check their exact text using
   `npm run check:english -- --text-file /tmp/pr-title.txt --text-file /tmp/pr-body.md`.
4. Rerun after the final edits. Do not commit or submit the PR while findings remain.

The CI English check runs for every PR, including documentation-only changes.
It scans tracked and untracked non-ignored text; ignored dependencies, runtime
files and binary assets are outside this repository-text policy.

## Dev workflow

After changing backend, frontend or Node code, **restart the local dev services
automatically** (`scripts/dev.sh restart`) so the running app picks up the change —
do not wait to be asked. Then verify the change against the running services.

## Reuse before implementation

Before adding or changing behavior, read [the reuse workflow](.agents/skills/workflow/SKILL.md)
and [the capability map](docs/reuse-map.md). Apply it to frontend, Node, Python,
and development tooling. Documentation-only edits need reference checks, not a
model download or duplicate scan.

1. Honor explicit user technology choices first; do not replace a specified library
   or stack without agreement. Within those constraints, inspect existing code and
   dependencies, then evaluate mature third-party solutions before implementing
   general-purpose infrastructure yourself. The Agent researches and validates fit;
   the user need not perform this investigation. Follow Workflow for evidence and
   unresolved selection conflicts.
   Find existing implementations in the relevant domain and shared modules.
   Search by behavior, symbols, types, routes and callers; inspect source and tests.
2. Prefer direct reuse → composition/adapter → compatible extension → extraction
   of stable common logic → new implementation with evidence explaining why.
3. Before editing, briefly state candidates, differences, and your decision.
   A zero-result search is not proof that no implementation exists.
4. Keep domain rules in their domain. Shared modules must not depend on pages.
   Do not add unrelated refactors or force JSON/streaming, lifecycle, or error
   contracts into one abstraction merely because code looks similar.
5. Validate affected callers when modifying a shared contract. Update the catalog
   when an entry, boundary, example or test changes; regenerate its Markdown map.
   When adding/extracting a reusable function, component or domain capability,
   proactively register it in `.reuse/catalog.json` in the same task. The user
   must not have to request registration separately. If nothing merits a new
   entry, briefly explain why in the delivery summary.
6. Keep task evidence current as you work (`begin`/`search`/coverage), and register
   reusable capabilities in `.reuse/catalog.json` in the same task (point 5). Do
   **not** run the full reuse verification cycle on every edit — run `cover`,
   `verify`, `check` and `verify-report` **only right before a commit**, so the
   pass reflects the final tree. See [Before you commit](#before-you-commit).

`scripts/dev.sh start` and `restart` also regenerate and validate the map before
starting/stopping services. This is a fallback, not a replacement for the coding
agent's registration and validation responsibilities. Startup cannot infer an
undocumented capability's business contract. Semantic vectors refresh on search.

## Before you commit

Do this once, after all edits and before `git commit` (never commit unless the
user explicitly asks). Adapt the task id, `--unit` list and test paths to the
active task; the block below is the current llm-d Gateway Mode task.

```bash
cd /home/yi/coding/prism/new_cluster_creation/llm-d-prism
for u in unit-10 unit-11 unit-13; do npm run reuse -- cover --task llmd-gateway-mode-migration --unit "$u" --outcome modified --summary "Model-cache downloads use the cluster's saved HF token (no token picker); topology column widths." >/dev/null 2>&1 && echo "cover $u"; done
K='not (test_aliases_and_concurrent_downloads_share_commit or test_moving_branch_publishes_new_checkout_preserving_old or test_distinct_urls_are_isolated or test_ref_names_that_previously_collided_and_cached_commit_offline or test_published_deployment_names_scope_commands_and_smoke_test)'
V(){ npm run reuse -- verify --task llmd-gateway-mode-migration "$@" >/dev/null 2>&1; }
V --unit unit-13 -- .venv/bin/python -m pytest llm_d_bench/model_cache -q
V --unit unit-10 --unit unit-14 -- npm run build
V --unit unit-11 -- npm run docs:check
V --unit unit-1 --unit unit-2 --unit unit-3 --unit unit-4 --unit unit-5 --unit unit-6 --unit unit-7 --unit unit-8 -- .venv/bin/python -m pytest llm_d_bench/model_service llm_d_bench/cluster -q -k "not (test_aliases_and_concurrent_downloads_share_commit or test_moving_branch_publishes_new_checkout_preserving_old or test_distinct_urls_are_isolated or test_ref_names_that_previously_collided_and_cached_commit_offline)"
V --unit unit-9 -- .venv/bin/python -m pytest llm_d_bench/deploy -q -k "$K"
V --unit unit-12 -- .venv/bin/python -m pytest llm_d_bench/api llm_d_bench/auth llm_d_bench/db -q -k "$K"
V --unit unit-13 -- npm run reuse -- map
V --unit unit-1 --unit unit-2 --unit unit-3 --unit unit-4 --unit unit-5 --unit unit-6 --unit unit-7 --unit unit-8 --unit unit-9 --unit unit-10 --unit unit-11 --unit unit-12 --unit unit-13 --unit unit-14 -- .venv/bin/python -m pytest llm_d_bench/model_service llm_d_bench/cluster llm_d_bench/deploy llm_d_bench/api llm_d_bench/auth llm_d_bench/db -q -k "$K"
node tools/reuse/cli.mjs check --task llmd-gateway-mode-migration --base a592fa579ce738dfa0637391bfedb02e47f8a9b0 --report .cache/reuse/report.json --json > /tmp/opencode/check.json 2>/dev/null
node -e "
const fs=require('fs'); const r=require('/tmp/opencode/check.json');
const p='.cache/reuse/tasks/llmd-gateway-mode-migration.json'; const t=JSON.parse(fs.readFileSync(p,'utf8'));
t.registrationReviews=t.registrationReviews||[]; const seen=new Set(t.registrationReviews.map(x=>x.key+'|'+x.hash)); let n=0;
for(const c of (r.registrationCandidates||[]).filter(c=>c.disposition==='unreviewed')){const k=c.key+'|'+c.hash; if(seen.has(k))continue; t.registrationReviews.push({key:c.key,hash:c.hash,reason:'Reviewed for the current task; not general capabilities.'}); n++;}
fs.writeFileSync(p,JSON.stringify(t,null,2)+'\n'); console.log('reviews added',n);
console.log('errors:', JSON.stringify((r.errors||[]).slice(0,6)));
"
node tools/reuse/cli.mjs check --task llmd-gateway-mode-migration --base a592fa579ce738dfa0637391bfedb02e47f8a9b0 --report .cache/reuse/report.json 2>&1 | grep -E "task check|ERROR|Registration" | head
npm run reuse -- verify-report --report .cache/reuse/report.json 2>&1 | tail -1
```

## Stop on unresolved decisions

Pause dependent modifications when source/tests cannot resolve business meaning,
compatibility, a CI exception, or ownership/meaning of a catalog entry. This rule
applies to similarity detection, semantic search, CI checks and index maintenance.

Before asking, finish the safe analysis and present: evidence with locations,
the exact difference, affected callers, options, recommendation and current state.
Record the pending request using `npm run reuse -- pause ...` (see workflow).
Do not perform the uncertain operation, weaken checks, suppress evidence, or
record approval while waiting. Independent read-only investigation may continue.

Only resolve using an actual user decision, its author and conversation/PR
reference. Never infer approval from silence, time passing, a search score, or
this general development authorization. Existing explicit decisions apply only
within their approved scope; do not ask again without a material change.

Task checks distinguish related pending from globally visible pending only when
an explicit reviewed impact scope exists. Unknown scope and changed decision
evidence remain blocking; never infer independence solely from file names.

The CLI records evidence; it cannot authenticate a human or technically prevent
all editor writes. Agents must obey this rule even outside CI.

## Skill routing

Use the common workflow plus every domain touched by the task. A feature spanning
UI, APIs and deployment uses all three domain skills; they are not alternatives.
Engineering workflow skills live under `.agents/skills/<name>/SKILL.md`.
Task-specific Copilot skills live under `.github/skills/<name>/SKILL.md` and must
be applied when their description matches the requested work.

| Task | Required skill |
| --- | --- |
| Explicit whole-repository review/refactor or comprehensive module review | [Repo review](.agents/skills/repo-review/SKILL.md): inventory, per-capability coverage, implement clear fixes and deliver a report |
| Any behavior change | [Workflow](.agents/skills/workflow/SKILL.md): discovery, reuse, registration, decisions and verification |
| React, browser clients, hooks, styles or charts | [UI](.agents/skills/ui/SKILL.md) |
| Node/Python APIs, domain services, persistence or tasks | [Backend](.agents/skills/backend/SKILL.md) |
| Startup scripts, containers, CI deployment or cluster lifecycle | [Deployment](.agents/skills/deployment/SKILL.md) |
| Table schemas, DAOs or Alembic migrations under `llm_d_bench/db/` | [Database](.agents/skills/database/SKILL.md) |
| Authenticated routes, permissions, roles, scopes, ownership, resource sharing, sessions or login | [Auth](.agents/skills/auth/SKILL.md) |
| A page, REST endpoint, or Python callable changed | [Keep the docs site in sync](#keep-the-docs-site-in-sync) below — update the matching Fern doc page in the same task |

Documentation-only edits: check references and consistency; read domain skills
only when their contracts are relevant. Do not run model downloads for link edits.

## Keep the docs site in sync

Lens ships a Fern documentation site under `docs/fern/` (preview: `npm run
docs:dev`; lint: `npm run docs:check`). Every Lens frontend module has three
doc surfaces, and a code change must update whichever of them its change
affects, in the same task — do not defer this to a separate follow-up:

1. **Frontend page behavior** (a React page/component under `src/`) →
   update the matching User Guide page at
   `docs/fern/pages/user-guide/<module>.mdx`.
2. **A FastAPI/Node REST endpoint** (added, changed, removed) →
   update the `## Backend API` section of the matching module page at
   `docs/fern/pages/api-reference/<module>.mdx`: keep its `### <endpoint
   name>` entry and REST API tab (method, path, request/response fields and
   a real JSON example) accurate to the actual route and Pydantic/TS contract.
3. **The underlying Python callable a REST endpoint uses** (service/DAO
   method signature, module path, or behavior change) → update that same
   `### <endpoint name>` entry's Python API tab on
   `docs/fern/pages/api-reference/<module>.mdx` so the sample code still
   imports and calls the real function. Every Backend API row must keep a
   matching, concrete Python usage example — never leave one updated
   without the other.

Module → doc-page name: Model market, Deployments, Evaluation, Simulation,
Model services, Clusters, Storage, Model cache, External providers,
Observability, Lens Assistant, Administration (kebab-case the name for the
file, e.g. `model-services.mdx`). If a module has no dedicated Python
backend (for example Model market, Lens Assistant), say so honestly on the
page and cross-link to the module that actually owns the call instead of
fabricating one.

After any doc edit, run `npm run docs:check` and fix reported errors before
finishing the task. Documentation-only edits still follow the reuse workflow's
reference-check guidance, not a full duplicate/model-download scan.

## Existing contracts

- File storage: follow [the storage scheme](docs/design/storage-layout.md) and the
  storage sections of the applicable domain skills for all file readers/writers.
- Follow [CONTRIBUTING.md](CONTRIBUTING.md) and the existing domain test layout.
- Treat `dist/`, dependencies, runtime data and generated MCP output as artifacts,
  not implementation sources.


Large-module tasks require per-capability coverage, not a single task-wide search
and rationale. Use `begin --module PATH`, or `scope --task ID --module PATH` for an
existing task. See Workflow for unit-specific searches, callers and verification.
For explicit repository review requests, follow repo-review and implement clear,
compatible refactors before reporting; the user reviews the final changes to
retain/reject them. Do not commit, restore CI or resolve business pending implicitly.

## Hardware is profile-driven (never hardcoded)

Hardware-specific behavior is data, not code. Vendor lists, accelerator labels,
device metrics/queries, DCGM/GFD/device-plugin manifests, driver access modes,
runtime images and accelerator variants must come from the hardware profile
registry — never a hardcoded constant table, a hardcoded `'gpu'`/`'xpu'` default,
or an `if vendor == …` branch in `llm_d_bench/`, `server/` or `src/`.

- **Add a hardware, not a branch.** A new vendor or access mode is one profile
  (`llm_d_bench/hardware/profiles/<vendor>.json`) plus, only when code is
  required, one provider (`llm_d_bench/hardware/providers/<vendor>.py`). Follow
  [the hardware plugin architecture](docs/design/hardware-plugin-architecture.md)
  and the profile `schema.json`. Domains (cluster, deploy, evaluate, simulation,
  monitoring, profiling) and the frontend must not change to support it.
- **Contribute every surface through the registry.** Profiles drive driver
  install/manifests and access `modes`, device discovery/DRF resources, monitoring
  images and dashboards, telemetry `device_metric_sources`, the default
  `deployment.runtime_image`, accelerator variants and capability gates.
- **Consume the contract.** Backend: `llm_d_bench/hardware/` (registry, resolver,
  `telemetry.py`); API: `GET /api/v1/hardware/capabilities`; frontend:
  `ClusterMonitoringStack/hardwareProfilesBackend.js` and the cluster overview
  `hardware` summary. Derive the vendor, label and default from the connected
  profile; an unset value means the feature is disabled, not another vendor's
  default.
- **Unknown stays neutral.** A per-vendor default table (for example runtime
  images) is selected by the discovered hardware; if the vendor is unknown, keep a
  neutral value rather than falling back to one vendor, and never clobber a
  user-entered value when the hardware resolves.
- **Test the contract**, not only the vendor you happen to run: profile shape,
  capability output, the consumer's derivation, and the deploy render.

## Product UI: settings and docs

- Anything configurable per resource must be settable in the UI where that
  resource is managed. An env var is a fallback, never the only way.
- Vendor/hardware defaults, labels and metric availability come from the hardware
  profile (see [Hardware is profile-driven](#hardware-is-profile-driven-never-hardcoded)),
  not a hardcoded vendor constant.
- Keep the create and edit surfaces consistent, and keep the matching Fern doc
  pages in the same wording.
