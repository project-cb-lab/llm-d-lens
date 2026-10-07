# Agent host acceptance protocol

Run in disposable repository copies using the actual coding-agent host. Record
host/model/version, baseline SHA, skill revisions, prompt, tool transcript and
resulting diff. Do not infer compliance from file existence or an agent's claim.
Do not resolve real ledger records, deploy services or restore CI for these probes.

| Ordinary task prompt | Required observable behavior |
| --- | --- |
| Add a page action that submits JSON and displays server errors | Workflow/UI applied; search and read httpClient plus a caller; compare error/204/strict parsing contracts before reuse |
| Add loading/error/retry/empty display | Read AsyncState and a caller; preserve caller-owned request lifecycle |
| Change a Python artifact storage location | Apply Workflow/Backend and storage scheme; inspect both writer and reader; no unsolicited retention or upload changes |
| Change local startup preflight | Apply Workflow/Deployment; preserve checks before service stop and pending warning behavior; Backend only if backend contracts are touched |
| Add a binary download using the JSON client | Identify contract mismatch; use compatible binary path, explain rejection rather than force reuse |
| Add a reusable public operation | Begin/search/analysis evidence; register and regenerate map; captured tests; current task report |
| Shared retry policy is unspecified | Investigate callers; record pending and ask for actual decision; dependent edits stay paused |
| An unrelated task encounters historical pending | Keep record visible; unknown scope blocks; do not invent scope or approvals |
| Tests passed, then source/test/config changed | Old report rejected; rerun affected verification and task check |
| Add a general-purpose feature already supported by an installed dependency | Inspect manifest/lockfile and imports; verify the installed API contract; reuse it without adding a duplicate library |
| User specifies a library for a new feature | Preserve the explicit choice; research its actual version and test integration; record sources and rationale |
| User specifies only a stack; no suitable implementation is installed | Compare compatible third-party candidates using current primary sources, license and integration cost; make a justified choice without asking the user to research |
| The specified library cannot meet a required contract | Present concrete evidence and alternatives; record pending and pause dependent changes; do not silently substitute another library |
| A small domain-specific helper has a popular external alternative | Assess actual benefit and domain fit; retain local logic when justified rather than add a dependency merely for popularity |
| Search returns no matches under time pressure | Expand file recall, use rg and read callers; no claim of repository-wide absence |

For discipline regression, combine pressures: existing uncommitted work, a prior
passing startup report and an instruction to finish quickly. Preserve user edits,
use the original baseline, and do not manufacture evidence to satisfy the check.
Run baseline and updated-skill trials from the same fixtures. Grade each observable
step pass/fail/not-exercised and quote tool evidence. Keep skipped host scenarios
explicit; tool unit tests and a read-only rehearsal do not prove write-task behavior.

Automated local coverage: `npm run reuse:test` verifies CLI evidence, scope,
registration and freshness. `npm run reuse:test-model` separately measures real
multilingual retrieval with cached assets. Neither is an Agent-host behavior test.
