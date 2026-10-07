# Unified Lens artifact storage

User authorization: current conversation approves the preceding storage proposal and requests complete implementation, including source version, configuration IDs, truncation and retention metadata. Work in the current dirty workspace, preserving all prior edits; do not commit unrelated changes or operate live services.

## Contract

Central Python paths and matching Node paths use LENS_DATA_DIR (XDG_DATA_HOME/lens), LENS_LOG_DIR (XDG_STATE_HOME/lens/logs), LENS_CACHE_DIR (XDG_CACHE_HOME/lens), LENS_SCRATCH_DIR (cache/tmp), LENS_RUNTIME_DIR (XDG_RUNTIME_DIR/lens or scratch/runtime). Only LENS_* roots and XDG defaults select storage locations. Legacy domain directory variables are ignored per the user’s 2026-09-18 request. Separate persistent artifacts/metadata/credentials from disposable caches. Explicit migration copies legacy files without deleting originals or overwriting different destination content; produce conflict reports and rewrite only known local path fields, never arbitrary strings. Do not automatically sweep or delete historical data on import.

Artifact manifests: artifact-manifest.v1 with owner_type, owner_id, created_at, updated_at, source_version (explicit unknown allowed), configuration_ids, retention_class, status, truncated, files (relative path, logical URI, media type, size, SHA256, truncation). Logical lens-artifact://<owner_type>/<owner_id>/<relative-path> identities are independent of root. Reject traversal/symlinks outside controlled roots. Atomic manifest updates. Domain lifecycle remains domain-owned. Publish metadata after payload; running logs can be incomplete. Do not infer full logs from tails or snapshots. Configuration outputs are immutable per artifact UUID; third-party filenames remain unchanged.

Evaluation, simulation, configuration and deployment must register their real persisted artifacts, including failure results. Other metadata stores use unified paths and keep existing business contracts. Model weights remain on registered cluster volumes in native Hugging Face layout; browser conversation/audio remain ephemeral (no new retention of private conversation). Pod KV trace remains task-local scratch, with durable collected evidence registered through evaluation. Developer reuse cache remains repository-local tooling state.

Retention classes are recorded, not automatic destructive deletion: configuration, evidence, diagnostic, cache. Provide retention inventory only; actual deletion uses existing domain deletion APIs so workflow references remain consistent. Preserve configuration and active work. Local filesystem is implemented storage backend; do not pretend object storage deployment has occurred.

## Validation

Tests isolate homes/roots; path precedence, manifests/checksums/traversal, migration conflicts and idempotence, domain integration and old API compatibility. Run Python regression tests, Node tests/type-check/build, reuse map/check, reference checks. Deliver old/recommended/implemented/future directory mappings.
