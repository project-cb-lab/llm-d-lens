---
name: repo-review
description: Review and refactor an entire repository or a large existing module when the user explicitly requests a comprehensive reuse/refactoring pass, then deliver verified changes and a coverage report.
---

# Repository review and refactor

This is the Agent execution entry. The user may invoke `repo-review` or ask to
“review the entire repository and implement unambiguous refactors”. Follow [workflow](../workflow/SKILL.md) and the
skills for each domain actually touched. This task authorizes compatible,
reversible refactors and validation; do not stop at a suggestions list or ask the
user to select every unambiguous change first. If the user explicitly requests
read-only review, respect that narrower scope.

## Inventory and preserve existing work

Inspect `git status --short` and existing differences. Preserve pre-existing edits;
do not reset, commit, push, deploy or migrate historical data as part of review.
Run `npm run reuse:review -- --task REVIEW_ID` once. If a task is already prepared,
resume it rather than overwrite it. The CLI prepares an inventory and instructions;
it does not invoke an LLM or claim a completed review.
For one module, use `begin --task ID --module src/features/example` (repeat
`--module` for additional roots). Existing tasks can use `scope --task ID --module
PATH` without resetting their commit baseline.

When the user asks to check all code against an old report, use the current
root inventory (`review`, without `--module`); the old report supplies findings,
not a scope boundary. Compare its named modules with the live inventory and review
new or previously unmentioned units too. Only an explicit report-item audit or
module-limited request permits that narrower claim.

Read the generated task JSON. Coverage includes Git-visible text, tests, scripts,
config and docs; excluded artifacts/binaries/large files are listed explicitly.
Directory groups are only a starting point: split `coverage.units` into actual
sub-capabilities (transport, storage, lifecycle, presentation, etc.), keeping IDs
unique and assigning every inventory file exactly once. Discuss shared dependencies
in each analysis even when ownership of a file belongs to another unit. Do not
mark an entire large module complete using one generic search and explanation.

## Work through every unit

1. Search each sub-capability with `search QUERY --task ID --unit UNIT_ID`; default
   six-file semantic recall remains suitable for candidate discovery. Enumerate
   the full inventory independently; six hits never define review coverage.
   Supplement with rg, read implementations, callers and tests. For general-purpose
   code, apply Workflow's technology-choice and third-party review: honor user
   constraints, inspect existing dependencies, evaluate suitable external solutions
   and record why to adopt one or retain the implementation. A library's existence
   alone does not justify replacing working code.
2. Record an `analysis` entry with `unitId`, `capability`, candidate paths,
   `callers` (or an explicit `callerNote` explaining no callers), selectedApproach
   and concrete contract rationale. Record cross-module relationships here.
3. Implement compatible changes immediately where source/tests resolve meaning;
   migrate affected callers and update catalog/map. Keep semantically different
   operations independent. Do not create refactors merely to make every unit show
   a change. For unchanged units, explain why the current design should remain.
4. If business meaning or compatibility remains unclear, record real pending,
   present the question and pause only dependent edits. Set that unit to blocked
   with the actual decision ID. Continue independent work; never invent approvals
   or narrow a legacy pending just to pass.
5. Assign new files and all changed files outside the initial module roots to units,
   including affected callers, catalog/map, package configuration and documentation. After analysis/edits, run
   `cover --task ID --unit UNIT_ID --outcome modified|kept --summary TEXT`.
   For blocked units use `--outcome blocked --id DECISION_ID`. This records current
   file hashes, not proof of reading. Deletion is reviewed as a null hash.
6. Execute suitable tests with `verify --task ID --unit UNIT_ID -- COMMAND`.
   A suite covering several units may repeat `--unit`. Final verification must
   match final code; run related suites again after later edits invalidate them.
   For docs/config-only units use relevant reference/syntax validation, not fake
   success commands. Coverage of business behavior remains a judgment to explain.

Use `review-status --task ID` after preparing/splitting inventory and at handoff
or resumption to see missing, stale and unassigned coverage without a duplicate
scan or model download. An incomplete unit remains Agent work. Progress output
is not a completed review, even when all coverage evidence is recorded.

## Deliver completed changes, not a proposal queue

Run a no-base `check --json` for whole-repository duplicate candidates, analyze
its full candidate list across units, and record decisions in analysis. This
scan does not replace inventory traversal. Run the task-baseline check to review
new public declarations; register capabilities or give hash-bound exclusion reasons.
Do not register every historical export just because full audit lists it.

Run `review-report --task ID`. It executes the existing task check and writes
`.cache/reuse/ID-review.md` plus JSON. Incomplete units and pending remain visible;
nonzero exit is not permission to suppress them. Run `verify-report --report
.cache/reuse/ID-review.json` before delivery. Report what changed, why, callers,
tests, unchanged units, excluded files and genuine blockers. Link the Markdown and
working-tree diff for the user to decide what to retain. Do not claim full
completion if any unit is unreviewed; excluded files are outside the claim.
