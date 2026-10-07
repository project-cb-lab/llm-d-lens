# llm-d-stacks Security Validation Plan

**Scope:** This plan covers code quality and static analysis, REUSE license
compliance, malware scanning, and Dependabot.

## Scope and Trust Assumptions

- Validation covers application code, build and automation scripts, dependency
  manifests and lockfiles, container definitions, and release artifacts
  submitted to this repository.
- Pull requests and dependency update PRs are treated as untrusted input.
  Validation runs on isolated GitHub Actions runners. PR checks must not expose
  deployment credentials or other long-lived secrets.
- GitHub Actions runners, scanners, and their signature/rule update channels
  are trusted infrastructure. Scan results apply only to the content scanned
  and do not establish the security of the runtime environment.
- The current PR workflow runs only for pull requests targeting `main`. Verify
  in GitHub repository settings whether required status checks and Dependabot
  security updates are enabled.

## Minimum Checks and Acceptance Criteria

### CQ-1: Code Quality

**Current state:** The [existing PR workflow](../.github/workflows/ci-pr-checks.yaml)
runs the frontend build, TypeScript type checking, JS tests, Python Ruff checks
and format checks, and a container build. [package.json](../package.json)
defines `npm run lint`, but CI does not currently run it.

**Minimum requirement:** Run the existing checks on code PRs and make
`npm run lint` a required CI check. A PR must not be merged if any check fails.
Exceptions require an owner, rationale, and expiration date. Build and test
checks may be skipped for documentation-only PRs, but REUSE checks must still
run.

### SAST-1: CodeQL Static Analysis

**Minimum requirement:** Run the official CodeQL security query suites for
JavaScript/TypeScript and Python on code PRs. Analysis failures block merging.
Triage findings before merge; high- and critical-severity findings block merge
unless a documented, time-bounded exception is approved. Keep CodeQL alerts and
query packs current, and retain SARIF output as the review artifact. A local
database run is useful for triage but does not replace the required CI check.

**Initial local run (2026-09-29):** CodeQL CLI 2.27.1 created JavaScript and
Python databases and completed the `security-extended` suites
(`codeql/javascript-queries` 2.4.6 and `codeql/python-queries` 1.8.11).
The run reported 18 JavaScript/TypeScript and 57 Python candidate results.
The SARIF result entries did not include severity levels; these counts are
untriaged query results, not confirmed vulnerabilities or severity counts.
SARIF files were written outside the repository to
`~/.cache/codeql/llm-d-stacks-javascript.sarif` and
`~/.cache/codeql/llm-d-stacks-python.sarif`.

### REUSE-1: REUSE License and Copyright Compliance

**Current state:** No REUSE check configuration or CI step has been identified.

**Minimum requirement:** Use the official REUSE tool to run `reuse lint` on
version-controlled repository files. Files must have valid SPDX license
identifiers and copyright information, or be covered by compliant license files
or annotations. Run this check on every PR, including documentation-only PRs;
failure blocks merging.

### MAL-1: Malware

**Current state:** No malware scanning configuration or CI step has been
identified.

**Minimum requirement:** Use ClamAV or an equivalent scanner to scan repository
content and the actual build artifacts being delivered on every PR and before
release. Also scan installed dependencies that are included in the deliverable
or runtime environment. Update signatures before scanning. Any detection fails
the check and blocks release. Manually investigate false positives and record
the file/hash, rationale, and disposition; do not use global exclusions.

### DEP-1: Dependency Vulnerabilities and Updates

**Current state:** The [Dependabot configuration](../.github/dependabot.yml)
currently covers only GitHub Actions and Docker, with weekly updates.

**Minimum requirement:** Add weekly update configuration for npm
(`package-lock.json`) and pip (`pyproject.toml`), retaining the Actions and
Docker updates. Enable and periodically verify Dependabot alerts and security
updates. Dependency update PRs must pass CQ-1, REUSE-1, and MAL-1. Prioritize
high and critical alerts; if an upgrade is not currently possible, document
mitigations, an owner, and a review date.

**REUSE definition:** REUSE in this plan means the REUSE.software license and
copyright metadata standard, not code reuse or duplicate-code detection. The
repository's existing reuse workflow and PR template continue to support code
reuse reviews, but do not replace `reuse lint`.

## Validation Methods and Traceability

- **CQ-1:** GitHub Actions checks code PRs, and branch protection requires the
  relevant checks to pass. The build, type-check, lint, tests, Ruff checks,
  format checks, and container build must all succeed; any failure is a fail.
- **SAST-1:** GitHub Actions runs CodeQL on code PRs for JavaScript/TypeScript
  and Python. Analysis must complete and publish SARIF; findings are triaged
  before merge, with high/critical findings blocking absent a documented
  exception.
- **REUSE-1:** GitHub Actions checks every PR. `reuse lint` must succeed;
  missing or invalid SPDX information is a failure.
- **MAL-1:** GitHub Actions scans every PR and release workflow with fresh
  signatures. No in-scope content may be detected; scanner failure, expired
  signatures, or failure to scan a deliverable is a failure.
- **DEP-1:** Dependabot alerts and weekly update PRs use the CI gates above.
  Alerts must be triaged and tracked, and required checks must pass before an
  update PR is merged. Unresolved high or critical alerts require documented
  temporary mitigations and a review date.

These cases trace only to the five security objectives in this plan. No
repository-level threat model or SDL task list has been identified, so this
plan does not claim broader threat coverage. If such a list becomes available,
map its specific threat/task IDs to the cases above.

## Configuration Verification and Maintenance

- Verify in GitHub repository settings that branch protection requires CQ-1,
  SAST-1, REUSE-1, and MAL-1 status checks to pass, preventing merges that
  bypass PR checks.
- Verify that Dependabot alerts and security updates are enabled, and that
  update configuration covers npm, pip, GitHub Actions, and Docker.
- Verify that malware scanning fails closed if signature updates fail or the
  scanner errors. The release workflow must scan the actual release artifacts,
  not only the source code.
- After changing scan configuration, use an isolated test with a detectable
  malware test sample to confirm that the scanner reports and blocks it. Never
  add the test sample to the repository or release artifacts.
- Repository maintainers review the alert backlog, scanner/rule update status,
  exception expiration dates, and required status-check configuration at least
  quarterly.
