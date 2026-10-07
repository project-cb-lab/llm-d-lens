# Additional Acceptance Evidence for the Agent Reuse Workflow

## Impact scope of the historical pending decision

Pending decision `ca783e76-c2cd-4880-b745-0f73ee1e5ff2` concerns retaining or deleting
`PageInfo`, `TaskRef`, and `TaskStatus`. This update adds only a source-reviewed impact
scope; it does not record user approval, delete definitions, or change business contracts.

Evidence:

- `llm_d_bench/schemas/common.py` defines pagination/task-reference models;
  `llm_d_bench/tasks/models.py` defines task statuses.
- Repository-wide symbol/module searches found only those definitions and the
  `TaskStatus` export in `llm_d_bench/tasks/__init__.py`, with no business callers.
  The enum remains separate from Simulation's own status type.
- Python dynamic-import sites were inspected; none dynamically load these models.
  Different file paths were not the sole basis for independence.
- `.reuse/catalog.json` registers neither these symbols nor their defining files;
  the PR did not modify their domain capability entries.
- `tools/reuse/repository.mjs` reads source and extracts declarations with Python AST;
  it does not import or execute the models. The CLI, review reports, and startup
  checks do not depend on their runtime contracts.

The scope includes both definition files and the package export, and explicitly covers
pagination, task-reference/status changes, and deleting these models. The export file is
also evidence: a content change invalidates the old scope. New business callers require
renewed analysis; this check does not prove future caller completeness.

The decision remains pending, with the original unknown-scope snapshot retained in history.
The current tooling task does not intersect the reviewed scope and may continue verification.
Tasks involving these definitions, exports, or operations still require the user's decision.

## Verification method

Rerun the recorded tooling tests, real offline model retrieval, shell syntax checks,
and documentation-reference checks against the final commit. Retain historical executions;
new results update the verification status of the same command without deleting failed or
stale records.

Acceptance depends on the current commit's task check and `verify-report`. Model tests
include Chinese-requirement retrieval and repository Recall@6/12 samples. Tool tests do
not replace actual Agent file-editing acceptance exercises.

## Agent host write-task acceptance (2026-09-20)

Independent Agents in the current Codex host actually edited files, executed commands/tests,
and paused; this was not a read-only walkthrough. The model family was GPT-6; the exact
model/host build version was not exposed. Node v22.23.2.

- Source: isolated local clone `/tmp/reuse-host-acceptance-repo`, commit
  `a18d6451c4996b217cf08813182d3ff4c15a5831`.
- Execution: `/tmp/reuse-host-fixture`, independent Git baseline
  `92ba777c2d084cd48a2a3ce9ea8fe1fe37002129`; copied the current CLI, Skills, and actual
  `src/utils/cn.js`, plus a two-file review fixture. Existing node_modules was reused read-only.
- Workflow SHA256: `052f55507889ad939f0a43cd406380304ead9ffcb429d958aa9bf354048c2041`.
- Repo-review SHA256: `313b935c2a42e4a73c255e714a128f14cbbd2f9051dc7a52ef233fe8b42a6326`.
- The main workspace, real decision ledger, and live services were unchanged.
  The fixture initialization commit served only as an independent baseline.

### 1. Existing dependency reuse and the user's technology choice

Controlled task: use clsx to add active styling and caller className to an existing
review row, preserving default output and Tailwind override behavior.

The Agent read Workflow, Repo-review, UI, the capability map, package.json/lockfile,
`cn.js`, and the real `Button.jsx` caller. Existing cn combines clsx and tailwind-merge.
It consulted the [clsx documentation](https://github.com/lukeed/clsx) and
[tailwind-merge documentation](https://github.com/dcastil/tailwind-merge). Installed
versions were clsx 2.1.1 and tailwind-merge 3.4.0, both MIT. It called existing cn,
adding no dependency and preserving the specified library.

It modified `src/features/audit/rowStyle.js` and added three regression assertions:
original default output, active/caller spacing override, and exclusion of false conditions.
Selection evidence and lookup date were recorded in task analysis.rationale; candidate
entries remained real repository paths. The function implements fixture-specific
presentation policy, so its registration candidate received an explicit hash-bound
exclusion rationale; shared composition remains owned by the existing cn entry.

```sh
node tools/reuse/cli.mjs begin --task host-write --module src
node tools/reuse/cli.mjs search 'cn clsx' --lexical --task host-write --unit composition
node tools/reuse/cli.mjs search auditRowClass --lexical --task host-write --unit presentation
node tools/reuse/cli.mjs verify --task host-write --unit composition --unit presentation -- node --test src/features/audit/rowStyle.test.js
```

All commands exited 0; actual test logs recorded 3 passed / 0 failed. Lexical search
was explicitly supplemented with rg and source reading; this scenario does not claim
semantic-model recall validation. No comprehensive dependency-security audit was performed
and no vulnerability-free guarantee is made.

### 2. Per-capability review and an intentional omission

Initial directory groups were split into composition (shared composition) and presentation
(caller/tests). Searches, callers, contract analysis, and tests were recorded separately.
Only composition coverage was initially completed; presentation completion was deliberately omitted.

| Actual command | Result |
| --- | --- |
| `check --task host-write --base 92ba777c2d084cd48a2a3ce9ea8fe1fe37002129 --report .cache/reuse/omitted.json --json` | Exit 1, INVALID; correctly reported missing review hashes and completed outcomes for two presentation files |
| `cover --task host-write --unit presentation --outcome modified --summary '…'` | Exit 0; recorded actual changes, rationale, and current hashes |
| `review-report --task host-write` | Exit 0; generated REVIEWED / CHECKED report |
| `verify-report --report .cache/reuse/host-write-review.json` | Exit 0; report was valid at that time |
| `git diff --check` | Exit 0 |

Subcommands above use `node tools/reuse/cli.mjs`. The initial empty fixture catalog was
rejected; acceptance began only after adding the existing cn entry in the required format.
Validation rules were not relaxed.

### 3. Pause when the specified library conflicts with the requirement

Additional independent controlled task: allow only clsx/lite while requiring it to directly
accept `{active:true}` and return active. Official documentation states that lite supports
only strings; the installed API was executed and returned an empty string for object input.

```sh
node -e "import('clsx/lite').then(({clsx}) => {if(clsx({active:true}) !== '') process.exit(1);console.log('clsx/lite object-input requirement conflicts: output is empty, expected active');})"
```

The check exited 0, confirming the conflict. The Agent then ran
`pause --kind semantic --file package.json --summary … --option … --option …`, presenting
existing cn/full clsx versus an approved object adapter. It exited 2, WAITING_FOR_USER.
The actual ledger retained pending `d7990a90-a63f-4b96-9cac-289bef6ecf2c`; no resolve,
library change, or conflicting implementation occurred. The parent Agent received the
pending decision explicitly; no end-user response was fabricated.

The subsequent `check --base <fixture baseline> --json` exited 2, preserving unknown-scope
blocking. Adding the decision changed repository content, so rerunning the earlier
`verify-report` exited 1: `Repository changed since this report`. The earlier passing
report therefore proves only the write-task state before the pause, not current validity.

### Reviewable evidence and limits

- Full commands, output, and exit codes: `/tmp/reuse-host-commands.log`.
- Actual source/test diff: `/tmp/reuse-host-write.diff`.
- Execution script: `/tmp/run-host-acceptance.py` (fixed temporary paths; use new paths
  for reruns rather than overwriting the existing baseline).
- Task JSON, omission report, passing report, and actual TAP logs:
  `/tmp/reuse-host-fixture/.cache/reuse/`.
- Evidence archive: `/tmp/reuse-host-acceptance-artifacts.tar.gz`.

This was a controlled small-module write task and fault-injection exercise. It establishes
that this Agent reused code, made actual edits, recorded per-unit coverage, handled check
rejection/correction, and paused. It is not whole-repository business acceptance, a Skill
A/B comparison, complete scenario coverage, or a cross-model adherence rate. Release/support,
security-advisory, and installation/migration workflows for an unknown new third-party
library were not validated.
