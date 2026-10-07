# Reuse-first development implementation plan

**Goal:** Deliver repository instructions, a maintained capability map, local semantic search, duplicate detection, CI validation, and an explicit human-decision handoff.

**Architecture:** Reuse Git for source discovery and change scope, TypeScript for JS/TS symbol discovery, and the already installed Transformers.js for local multilingual embeddings. Keep deterministic failures, advisory candidates, and pending human decisions distinct. No application behavior changes.

**Approved design:** The user approved the complete reuse-first workflow discussed in this conversation, including pausing dependent work when behavior, compatibility, or exceptions need a human decision.

## Constraints

- Source remains local; only model assets may be downloaded.
- Exclude generated, ignored, dependency and test files from implementation search.
- Similarity is evidence, never automatic authorization to refactor.
- No commits, publishing, or external messages are needed for this task.

## Tasks

- [x] Test and implement `tools/reuse/repository.mjs`: Git discovery, source chunks, catalog validation, deterministic map rendering and duplicate candidates. Test with temporary Git repositories, renamed copies, deleted symbols and out-of-scope paths.
- [x] Test and implement `tools/reuse/search.mjs`: normalized vector ranking, incremental embedding cache, model identity invalidation, real local multilingual inference. Separate pure cache/ranking tests from the model integration smoke check.
- [x] Test and implement `tools/reuse/cli.mjs`: index/search/check/map commands and persistent pending/resolved human decisions; test exit codes and ensure no command silently approves a pending request.
- [x] Add `AGENTS.md`, `.agents/skills/workflow/SKILL.md`, `.reuse/catalog.json`, generated `docs/reuse-map.md`, usage documentation and PR evidence fields. Preserve existing style and Docker guidance.
- [x] Add npm commands and a read-only PR CI job that checks the catalog, publishes duplicate evidence and blocks unresolved human decisions without attempting fixes or bypasses.
- [x] Run final focused tests, repository check, actual semantic search, type check and relevant existing tests. Inspect diff and report limitations accurately.

## Verification

- Existing frontend/Node suite: 282 passed.
- Reuse unit/CLI suite: 18 passed, including missing symbols, Python docstrings,
  invalid bases, untracked source, pending decisions and changed approval evidence.
- Real offline model integration: 2 passed, including repository retrieval of
  `llm_d_bench/common/json_store.py::write_json_atomic` from a Chinese requirement.
- Type check, focused ESLint, workflow YAML parse and diff whitespace checks passed.
- Incremental repository check: 370 source files, 5 changed implementation files,
  no candidate pairs and no pending decisions for this task.
- Independent read-only code review: no blocking findings.
- GitHub workflow was validated locally, not run remotely. Required status checks
  need repository administrator configuration after the workflow is published.
