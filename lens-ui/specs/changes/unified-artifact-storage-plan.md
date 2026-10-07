# Unified Artifact Storage Implementation Plan

**Goal:** Implement the user-approved unified file storage and provenance contract in one delivery.
**Architecture:** Shared path selection and artifact manifests; domain adapters retain lifecycle; explicit non-destructive migration. Existing filesystem stores remain compatible.
**Spec:** unified-artifact-storage.md

- [x] Root: test then implement llm_d_bench/utils/paths.py storage_path(area, *parts), preserving prism_temp_root; only XDG/LENS roots determine storage. Legacy directory variables are ignored per the user’s 2026-09-18 request.
- [x] Root: test then implement llm_d_bench/utils/artifact_store.py register_artifacts(root, *, owner_type, owner_id, source_version=None, configuration_ids=(), retention_class='evidence', truncated=False, files=None, status='complete'). Return and atomically persist manifest.json. files optionally maps relative file paths to per-file metadata (truncated/kind); otherwise inventory regular files recursively excluding own manifest and temporary files. Explicit unknown provenance is honest.
- [x] Evaluation integration: unify records/results roots, register saved evidence on terminal execution, preserve failure/cancellation, meaningful provenance/truncation, tests.
- [x] Simulation integration: unify task/dataset/backend/tokenizer paths, register completed/failed task artifacts and downloaded datasets, provenance/truncation, tests.
- [x] Configuration/deployment integration: immutable configuration UUID directories; register configuration snapshots/manifests and deployment diagnostic archives, unified paths, regression tests.
- [x] Root: migrate remaining metadata/bootstrap/repo/monitoring roots and Node counterparts; scripts and Docker persistent/cache/log volumes use same layout.
- [x] Root: explicit migration inventory/apply CLI, copy only without overwrite, known persisted path rebasing, report conflicts; retention inventory avoids automatic destructive expiry.
- [x] Review full diff; run domain regressions, Node checks, build, reuse registration/map/check; write actual four-part solution and limitations.

Use test-driven-development for new shared contracts; existing regression suites verify compatible caller migrations. Parallel domain workers own disjoint files; no live data deletion, service restarts, or git commits.

## Delivery verification

Implementation and domain review completed. Existing historical data is not moved while services may be writing; offline migrate/reindex tooling and dry-run results are delivered. Retention inventory intentionally delegates deletion to domain APIs. Prior dirty workspace and pending ownership decision were preserved.
