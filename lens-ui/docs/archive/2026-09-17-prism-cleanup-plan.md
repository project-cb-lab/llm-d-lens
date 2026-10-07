# Remove upstream Prism features

Goal: retain the ten Lens pages and all their transitive dependencies, while removing upstream-only features compared with `/home/wenjiao/offical/llm-d-prism`.

The working tree already contains the removal of upstream dashboards, Results Store, OAuth, data ingestion, archived data, and their packages. Preserve those edits. Keep evaluation subroutes, deployment planning, Python APIs, MCP, shared UI, and runtime data.

- [x] Remove unused upstream UI exports (`FactCell`, `StatPills`) after checking consumers.
- [x] Remove the placeholder Go service and replace its Make/CI targets with existing Node/Python checks.
- [x] Remove the original site's Cloud Run publisher, production config, and publishing documentation. Keep Lens containers and cluster deployment code.
- [x] Remove upstream feature specifications/personas and update references in contributor/style guidance. Keep specifications added for Lens.
- [x] Update stale evaluation test assertions to match the smaller presets introduced by commit `994aba6`; do not change evaluation behavior.
- [x] Verify build, TypeScript, unit tests, Python tests, local imports, API retirement, and retained page rendering. Record limits and results in the cleanup report.
