# Module coverage and repository review

User-approved intent: require per-capability analysis for large modules; offer an
Agent-host entry for repository review that implements unambiguous refactors and
reports completed changes for the user to retain/reject. No automatic commit,
startup review, CI restoration or pending approval. Preserve pre-existing edits.

Reuse task JSON, captured searches/tests, input fingerprints and existing checks.
Add optional coverage for bounded tasks and mandatory coverage for full review.
`begin --module PATH` inventories a module; `review --task ID` inventories the
repository and creates a host instruction document. `repo-review` skill is the
actual Agent execution entry: terminal CLI alone does not invoke an LLM.
Inventory Git-visible text, including tests, scripts and documentation; explicitly
list excluded binaries, symlinks, large files and generated artifacts. Group by
directory as a starting point; agent splits groups into real sub-capabilities.
Every inventory file must belong to a unit; new files require assignment, removed
files require a recorded review. Each unit needs its own search, analysis/callers,
result and verification; reviewed hashes expire on changes. Unknown pending stays
blocking. Report incomplete work explicitly, never imply a whole-repo completion
from six search hits. Reports distinguish changed/kept/blocked/unreviewed units.

Plan:
1. Add CLI regression tests for inventory, incomplete coverage, new/deleted files,
   per-unit search/test evidence, and truthful Markdown reports.
2. Add review.mjs using repository state and existing evidence. Integrate review,
   begin --module, --unit search/verify/check and review-report commands.
3. Add repo-review skill and Workflow/AGENTS routing, examples and catalog entry.
4. Run focused/full tool tests, review regressions and task-baseline reuse check.


Implementation notes: full review checks all source duplicate candidates while
registration remains task-diff scoped. Start snapshots preserve out-of-scope edits
for late scope attachment; legacy tasks conservatively use baseline differences.
The 10-source fallback counts deletion as well as additions/modifications.
Agent-host repo-review is the execution entry; npm reuse:review only prepares the
inventory and explicit handoff. Error paths replace Markdown with INCOMPLETE.


Validation: isolated Agent-host write rehearsal actually removed a duplicate by
canonical delegation, preserved a public facade, registered capabilities and ran
real node:test. It initially caught unassigned catalog/map edits outside the module;
after assigning them and registering the facade, review-report and freshness were
CHECKED. Fixture was removed; no main-repository business files were changed by
that rehearsal. This is not a full-repository behavioral guarantee.
