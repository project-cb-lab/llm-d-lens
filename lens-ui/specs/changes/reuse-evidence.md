# Reuse evidence and check boundaries

Approved scope: implement the seven improvements discussed in this conversation.
Task baseline: 8574b1f561618897dfbe2a79eeff2032c925845e.

## Design

Extend the existing reuse CLI, repository discovery, decision ledger and ranking.
Keep business approvals intact and do not restore the removed CI workflow.
Task evidence lives under ignored `.cache/reuse/tasks/<id>.json`; reports and
command logs also live under `.cache/reuse/`. Export these artifacts for review.
Evidence is audit data, not authenticated proof or a global editor lock.

Checks distinguish audit/startup/task purposes. Task checks require a pinned
baseline, skills, search evidence, candidate analysis, registration dispositions
and successful command verification for the current repository fingerprint.
Fingerprint Git-visible files (including tests, docs, config and deleted files),
HEAD and task evidence; exclude reuse output/cache to avoid self-reference.
Ignored runtime files are outside the guarantee. Reject stale/legacy reports.

Pending scope is optional and explicit: files, capability IDs, callers,
operations and a reviewed rationale. Without scope, or with changed evidence or
unknown capability IDs, block conservatively. Include all pending in reports.
Only task checks with recorded operation scope can distinguish unrelated pending;
audit/startup retain the existing conservative behavior. Related files include
capability source/examples/tests and declaration evidence, not just direct edits.
No automatic scope migration or resolution of historical records.

Public declaration review covers JS/TS exports (including re-exports/defaults)
and Python top-level non-private functions/classes. It is advisory coverage,
not an exhaustive public API analysis. Registered declarations link to catalog;
unregistered candidates require a hash-bound exclusion reason in task checks.
Existing registered capabilities whose implementation changes also appear.

Search has independent file-limit and result-limit parameters; preserve default
six-file recall, report scope and collect task search provenance automatically.
Add real repository Recall@K samples and a host behavior scenario protocol.
Do not report host/model tests as executed unless they actually ran.

## Implementation plan (inline)

- [x] Add failing CLI tests for report freshness, task verification, scoped pending,
  registration review, evidence reasons and search bounds. Run them before code.
- [x] Implement state snapshots in tools/reuse/evidence.mjs and public declaration
  candidates in tools/reuse/registration.mjs; extend decisions.mjs and search.mjs.
- [x] Integrate begin/verify/verify-report and task-aware check/search in cli.mjs.
  Preserve old audit exit codes and startup warning behavior.
- [x] Mark startup purpose in dev.sh; make summary reject outdated reports.
- [x] Register capabilities; update workflow, AGENTS and operational documentation.
  Add retrieval samples and host behavior acceptance scenarios.
- [x] Run tool tests, semantic tests, shell syntax, catalog/map validation and
  task-baseline reuse check. Analyze all duplicate candidates and review diff.

## Review focus

Legacy scope must not bypass pending. Deletion/re-export/new untracked source must
be visible. A failed rerun must supersede an older passing result. Report freshness
must include task edits and non-source inputs. Malformed scope and evidence must
fail closed, without mutating historical decisions. A fresh report is not itself
proof that the task passed or the agent obeyed every instruction.


## Execution notes

Reused existing CLI/discovery/ranking/ledger; registered four tooling capabilities.
Initial CLI regressions failed (5 new failures); implementation and subsequent
regressions cover stale logs, default/re-export identity, failed report replacement,
caller/catalog scope and executable-mode changes. Independent review reproduced
three boundary defects; fixes have dedicated regression cases.

The existing semantic smoke expected `llm_d_bench/common/json_store.py`, absent
also at the task-start commit. Retired that obsolete repository target in favor
of the current JSON HTTP capability (postJson delegates to requestJson) and real
artifact-inventory requirements; kept the real-model test and Recall@K assertions.
This does not recreate a withdrawn generic storage capability.

Legacy pending remains untouched and blocks task delivery checks with exit 2.
CI restoration remains outside this implementation per the existing explicit
removal decision. Host protocol is provided; a read-only host rehearsal covers
candidate/decision reasoning only, not full write-task behavior or all future hosts.
