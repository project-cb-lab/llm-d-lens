---
name: workflow
description: Check existing repository capabilities before adding or modifying behavior; escalate unresolved business, compatibility, CI or catalog decisions.
---

# Workflow

Paths and commands below are relative to the repository root. Apply the relevant
UI, backend or deployment skill selected by [AGENTS.md](../../../AGENTS.md).
Documentation-only edits require reference checks, not semantic indexing.

## Before changing code

Record the task-start commit with `git rev-parse HEAD`. Start task evidence before
implementation: `npm run reuse -- begin --task TASK_ID`. Read `docs/reuse-map.md`.
Resume an existing task rather than resetting its baseline to hide committed work.
Fill `.cache/reuse/tasks/TASK_ID.json`: `applicableSkills` (repo-relative paths),
`operations` (explicit operation names) and `analysis` (capability, candidate paths,
selectedApproach: reuse/adapt/extend/extract/new, rationale including contract differences).
These are agent-authored judgments, not proof of reading or authenticated approval.
Break the request into capabilities (transport, validation, storage, UI state,
domain operation), then search each capability:

```bash
npm run reuse:search -- "describe the intended behavior" --task TASK_ID
rg -n 'relatedSymbol|relatedRoute|domainTerm' src server llm_d_bench tools scripts
```

Search automatically refreshes the local semantic index. First run downloads the
pinned multilingual model; inference and source stay local. An unavailable model
is a reported tool failure, not permission to assume no reuse exists. Use explicit
`--lexical` plus `rg` if needed, disclose reduced coverage, and pause if that leaves
a material decision unresolved. Use `--offline` after the first model download.

Search automatically records query, mode, result paths and input fingerprint in
task evidence. Default semantic recall is six distinct files; use `--file-limit 12`
or a larger value (maximum 100) when candidates are insufficient. `--limit` controls
returned chunks, not file recall. Zero results require supplementary symbol/route/
caller searches; lexical mode is whitespace substring matching, not Chinese segmentation.
Read the candidates, real callers and tests. Compare inputs/outputs, errors,
side effects, lifecycle, performance and dependency direction. Summarize evidence
before implementation in a short table: capability / candidate / difference /
decision. State search scope and nearest rejected candidate for new code.

## Technology choices and third-party reuse

Honor the user's explicit stack, library and version constraints. A specified stack
still leaves compatible library choices open; a specified library must not be
silently replaced. Within those constraints, inspect repository implementations,
dependency manifests, lockfiles and actual imports before considering a new library.
For general-purpose capabilities, evaluate suitable mature third-party solutions
before writing a replacement. Existing suitable dependencies take priority over
adding another library for the same purpose. This investigation is the Agent's job.

When assessing an external candidate, consult current official documentation,
release/support information, license and relevant security advisories for the
actual version. Check required behavior (including errors, cancellation and side
effects), runtime compatibility, transitive dependencies, bundle/deployment cost
and migration impact. Popularity alone is not evidence of suitability. Use focused
integration tests when adopting a library, and update the appropriate manifest and
lockfile together. Do not replace working code across unrelated modules or add a
large dependency for a trivial helper without a concrete benefit.

Record the user's constraints, inspected dependencies/versions, external candidates,
source URLs and lookup date, acceptance/rejection reasons and validation in the
existing analysis `rationale`. Keep `candidates` as repository paths (such as the
manifest and relevant caller); do not put package names or URLs in this path field.
Keep selectedApproach as reuse/adapt/extend/extract/new. Explain why a custom general-
purpose implementation is needed; domain-only changes can briefly explain why an
external library evaluation is not applicable. No new evidence schema is required.
The CLI checks record structure, not the truth or completeness of library research.

Proceed with clear technical choices inside the authorized task. If a specified
choice cannot meet requirements, or concrete compatibility, license or security
findings require changing it, present evidence and alternatives and pause dependent
changes for the user's decision. Do not treat popularity or missing network access
as grounds to override the choice. Disclose unavailable research; do not claim
current maintenance or safety without evidence. Unapproved paid services, external
data transfer or unresolved business tradeoffs also require a user decision.
Use the existing handoff (`pause --kind semantic` for an unresolved selection),
with local manifest/caller evidence and external findings in the summary.

## Choose the smallest compatible change

- Same contract: call the existing implementation.
- Same core behavior, different boundary data: compose or adapt.
- Missing legitimate variant: extend compatibly and verify old callers.
- Stable common behavior in multiple callers: extract and migrate those callers.
- Different domain meaning or lifecycle: keep separate, sharing lower-level parts
  where useful. Explain why; do not claim a numerical score proves equivalence.

Keep domain code local before promoting it into shared infrastructure. Follow
the existing UI contract; do not create another generic utility directory.

## File storage changes

When a task creates, downloads, reads or deletes application files, read the
[storage scheme](../../../docs/design/storage-layout.md). Apply its directory, naming
and reference rules to existing and new features. Find both writers and readers,
including download/delete endpoints and launchers, before changing a path.
Validate their agreement with isolated local files and domain tests. Real-cluster
acceptance is separate from path-design verification and is not a prerequisite.
Storage-path work does not itself authorize new recording uploads, archives,
automatic deletion, source deduplication or historical migration.

## Shared file placement and naming

Before creating or extracting a shared module, apply the naming table in
[ui](../ui/SKILL.md) or
[backend](../backend/SKILL.md), as appropriate.
Use the [shared code guide](../../../docs/refactoring/shared-code-guide.md)
for the existing inventory and directory conventions.

- State the target path and exported symbol with the reuse decision.
- Use the existing capability owner; do not introduce duplicate `common`,
  `helpers` or `utils` directories for the same responsibility.
- Keep domain rules in their domain and shared modules independent of pages.
- Apply conventions to new files. Preserve established public imports and
  existing file extensions; unrelated legacy renames are outside the task.
- Before delivery, check filenames, exported symbols, test names, imports and
  catalog paths. Keep the guide and affected skill tables consistent when
  changing a naming convention. Record capability IDs in `kebab-case`.

## Human decision handoff

Pause when any of these remains unresolved after safe investigation:

| Source | Decision requiring the user |
| --- | --- |
| Similarity | Whether differing business behavior should be unified; whether to change an existing contract |
| Semantic search | Which plausible capability matches an unspecified requirement |
| CI | Whether a suspected false positive warrants a specific rule exception |
| Catalog | Whether documentation or implementation expresses the intended ownership/contract |

Provide locations, differences, affected callers, options, recommendation, what
has already happened, and which edits are paused. Record the same evidence:

```bash
npm run reuse -- pause --kind similarity \
  --summary "Existing operation retries; new behavior is unspecified" \
  --file src/api/httpClient.js \
  --option "Keep existing behavior and adapt the caller" \
  --option "Change the shared contract and migrate callers"
```

For a reviewed, bounded decision, `pause --scope scope.json` accepts explicit
`files`, `callers`, `capabilityIds`, `operations`, `rationale`, `reviewedBy` and
`reviewRef`. Use real caller/contract analysis; an empty file intersection alone
cannot establish independence. Scope is an impact statement, not business approval.
Omit scope when uncertain. Legacy records stay unknown; never retroactively assign
scope just to pass a check. Task checks keep all pending visible but block only
related or unknown scope; audit/startup checks remain conservative.

Exit 2 means WAITING_FOR_USER. Send the question to the user; merely writing a
ledger record is not a notification. Do not execute the dependent edits while
waiting. Read-only analysis and independent authorized work can continue.

After an actual answer, record it **before** changing the evidence files:

```bash
npm run reuse -- resolve --id REQUEST_ID \
  --decision "The user's actual decision and approved scope" \
  --approved-by "Actual user/reviewer" \
  --approval-ref "Actual conversation message or PR review reference"
```

Never manufacture these fields or delete a pending request to unblock CI.
The ledger is an auditable record, not identity verification. If evidence changed
before the user answered, run `pause --id REQUEST_ID` with the updated kind,
summary, files and options. This preserves history and keeps the request pending.
Present the updated scope and obtain a decision for it; do not reuse the old
answer. Historical resolved records are not
blanket permission for later tasks.

Clear cases continue without redundant approval. Zero findings and passing CI
never remove the obligation to pause on uncertainty discovered by reading code.

## Finish and report

1. Proactively register newly added/extracted reusable functions, components and
   domain capabilities in `.reuse/catalog.json`, and update existing entries when
   their contracts change. When a registered capability is moved or removed,
   update or retire its entry and check linked examples, tests and documentation. Run `npm run reuse:map`; do not ask the user to run it
   or remind you to register capabilities. If no entry is needed, explain why.
   A changed path or a one-off private helper alone does not need a public entry.
2. Run an audit check with `--base TASK_START_SHA --json` to inspect
   `registrationCandidates`. Register reusable capabilities; for a one-off public
   declaration, add `{ "key": "path#symbol", "hash": "candidate hash", "reason": "specific reason" }`
   to task `registrationReviews`. Reasons expire when the file changes. Detection
   is not exhaustive (for example dynamic exports); review the actual diff too.
3. Execute relevant regression tests through
   `npm run reuse -- verify --task TASK_ID -- npm run reuse:test` (replace the
   command with the actual domain tests). It records argv, exit code, code
   fingerprints and a hashed log. Do not fabricate results or erase failed runs;
   rerun the same command after a fix. Explain missing coverage.
4. Run `npm run reuse:check -- --task TASK_ID --base TASK_START_SHA --report .cache/reuse/report.json`.
   Analyze all similarity candidates, including existing duplicates in changed files.
   Do not repair unrelated historical duplication as part of the feature.
   Before delivery run `npm run reuse -- verify-report --report .cache/reuse/report.json`.
   Changes to tests, skills, configuration, code, HEAD or task evidence invalidate
   the report; rerun affected verification after changes. A fresh WAITING_FOR_USER
   report still exits 2. Startup reports are never full task evidence.
5. Report reused/extended/extracted entries, new implementation rationale, checks,
   and any user decisions. Complete the PR's reuse section when creating a PR.

See [the tool guide](../../../docs/refactoring/reuse-first-agent-design.md) for scope, exit codes and CI behavior.
`scripts/dev.sh start/restart` regenerates and checks the map as a fallback before
service changes; agents still register and validate capabilities during the task.

Task evidence and command logs are ignored local artifacts. Attach/export the task
JSON, report and relevant logs with a review; they are not retained by a normal
Git commit. Never put secrets in commands or evidence. Stage records correspond to
DISCOVERY (begin/search), REUSE_ANALYSIS (analysis), DECISION_GATE (ledger),
IMPLEMENTATION (Git diff), REGISTRATION (candidate dispositions) and VERIFICATION
(captured command results/report). Their existence does not enforce editor order.
Host behavior acceptance scenarios: [protocol](../../../specs/changes/reuse-agent-scenarios.md).


## Large modules and comprehensive review

For large modules, begin with `--module PATH` (repeat for multiple roots); attach
coverage to an existing task using `scope --task ID --module PATH`. Split directory
groups into sub-capabilities before implementation and assign every inventory file.
Each unit requires its own tagged search (`--unit ID`), analysis with `unitId` and
callers, recorded outcome/file hashes (`cover`), and tagged verification. New files,
removed files and changed hashes must be accounted for. Ten or more changed source
files automatically require coverage; smaller but semantically large modules also
require it by this rule. This threshold is a fallback, not a definition of complexity.

When the user requests a whole-repository review/refactor, use
[repo-review](../repo-review/SKILL.md). Complete compatible, unambiguous fixes and
validation before delivery; do not stop at recommendations awaiting selection.
Only unresolved business/compatibility decisions pause dependent edits. The user's
explicit read-only constraint, if any, takes precedence. Keep default search recall
at six; full review walks the inventory rather than treating search hits as coverage.

For whole-code requests accompanied by an old report, follow repo-review's live
root inventory rule. Do not turn a historical findings list into the review scope.
Use `review-status` for interim coverage; final `review-report` and freshness checks
remain required before claiming completion.
