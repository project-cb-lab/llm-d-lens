# Contributing to Lens

Scope changes to the 10 pages and supporting workflows listed in
[README.md](README.md).

## Issues and pull requests

Use this repository's issues for bug reports and feature requests. Include
reproduction steps, expected and actual behavior, the commit or version, and
relevant environment details. Remove credentials and private data from logs.

Describe the problem, resulting behavior, and validation in each pull request.
State any known failures or untested external integrations. Follow the shared
UI conventions in [.agents/skills/ui/SKILL.md](.agents/skills/ui/SKILL.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Repository language

Write all repository content and PR titles/descriptions in English. Run
`npm run check:english` before committing or opening/updating a PR. Agents must
translate findings automatically, review their meaning, and rerun until clean.
Use `-- --text-file /tmp/pr-title.txt --text-file /tmp/pr-body.md` to include
PR metadata in the check. The scanner reports Han characters in Git-visible text
and filenames; it is not a natural-language classifier for every other language.
Do not replace text with escapes or remove useful content to bypass the policy.

## Development and validation

Before changing behavior, follow [AGENTS.md](AGENTS.md) and the
[reuse-first workflow](docs/refactoring/reuse-first-agent-design.md). Find existing implementations,
compare contracts and callers, and record reuse/new-code rationale in the PR.
Pause dependent work for unresolved business, compatibility, CI exception or
catalog decisions; obtain and record the user's actual answer before proceeding.

Run `npm run reuse:test` for reuse-tool changes and
`npm run reuse:check -- --task <id> --base <task-start-commit> --report .cache/reuse/report.json`
for behavior changes. Follow the workflow's `begin`, `search --task` and `verify`
evidence steps; run `verify-report --report .cache/reuse/report.json` before delivery.
Export the ignored task evidence and command logs for review. Reports are bound to
Git-visible inputs; ignored runtime data is outside their validity guarantee.
Update `.reuse/catalog.json` and run `npm run reuse:map` when capability entries
change. Similarity candidates require analysis, not automatic refactoring.

Follow [README.md](README.md) for setup. Choose checks appropriate to the change:

```bash
npm run build
npm run type-check
make test-js
make test-python
make lint
```

Document substantial feature designs under `specs/changes/`. Keep API descriptions
and relevant documentation in sync with frontend and backend behavior. Real
cluster deployment and external provider calls require a configured environment;
report separately whether those flows were exercised.

Preserve the upstream Apache 2.0 [license](LICENSE) and source copyright notices.
