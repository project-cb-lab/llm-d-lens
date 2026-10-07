# Reuse-First Agent: Design and Usage Guide

## 0. Design overview

> **Core principle: search for and reuse existing implementations first, review large modules item by item, and verify against the final code before delivering an auditable change report.**

This solution is intended to reduce duplicate implementation, missed checks, and ineffective delivery evidence in AI-assisted development. It is executed by the existing coding assistant and does not introduce a separate backend Agent service.

### 0.1 Applicable scenarios

| Task type | Processing flow | Deliverables |
| --- | --- | --- |
| Developing new features | Find existing implementations, compare calling patterns and business meaning, then reuse, extend, or add new code | Feature code, reuse rationale, and verification results |
| Refactoring an existing large module | Regardless of code origin, build an inventory of files and sub-capabilities, analyze them item by item, and implement explicit compatible refactors | Module changes, rationale for retained parts, coverage, and test results |
| Reviewing and refactoring the entire repository | Proactively traverse the file inventory, analyze cross-module duplication, complete explicit modifications and caller migration | Change report, full coverage inventory, and unfinished items |

**Ordinary development is limited to the task-relevant scope; whole-repo reviews must be explicitly initiated by the user.** When a read-only review is explicitly requested, the Agent analyzes only and does not modify.

### 0.2 Principles of reuse and extraction

| Conclusion from source, caller, and test analysis | Handling approach | Example |
| --- | --- | --- |
| Existing code satisfies the need | Call it directly | A new page uses an existing JSON request function |
| Core behavior is the same, but input/output shape differs | Compose or adapt | Convert page data and pass it into an existing validation function |
| The same capability lacks a reasonable variant | Extend with backward compatibility | Add an optional parameter while preserving the original default behavior |
| Stable common logic exists in multiple places | Extract a shared capability and migrate callers | Multiple pages share loading, failure, and empty-data display |
| Business meaning or lifecycle differs | Keep separate | Retry for query failures and retry for paid operations are handled separately |
| No suitable implementation exists | Add a new implementation and explain why the closest candidate is not applicable | Add a new domain computation capability |

> **Code similarity is only a candidate clue and cannot by itself justify merging.** Reuse decisions should evaluate input/output, error handling, side effects, lifecycle, and dependency direction together.

**Third-party implementations are also reuse candidates.** First honor the user’s specified stack, libraries, and versions; then check project code and existing dependencies; when no suitable implementation exists for a general capability, the Agent researches mature third-party solutions before considering building one in-house. Do not replace a library explicitly specified by the user on your own, and avoid introducing an overly costly dependency for simple logic.

The Agent reviews official documentation for the target version, maintenance status, license, and security advisories; compares functional contracts, environment compatibility, dependency cost, and migration impact; and runs relevant verification after adoption. If the technology choice is explicit, implement it directly; if the specified option conflicts with the requirement, provide evidence and alternatives for the user to decide.

Selection rationale is written into the existing task analysis `rationale`: user constraints, existing dependencies and versions, third-party candidates, source links and review dates, reasons for adoption or rejection, and verification results. `candidates` still records repository paths, such as dependency manifests and callers; do not fill it with library names or external URLs. When no general-purpose library is suitable for business rules, briefly explain why without doing unrelated searches. The CLI does not automatically validate whether third-party research was sufficient.

### 0.3 Responsibilities and authority

| Party | Responsibilities | Boundary of responsibility |
| --- | --- | --- |
| User | Clarify goals, decide business ambiguities, accept changes | Does not need to manually maintain the catalog or approve every explicit compatible refactor item by item |
| Agent | Break down tasks, inspect code, decide reuse approach, implement modifications, register capabilities, run verification | Explicit compatible changes should be implemented directly without waiting for approval item by item |
| Local tools | Record searches and tests, check registration and coverage, detect duplicate candidates, identify stale reports | Cannot independently determine all business semantics or prove the Agent’s analysis is correct |

**Changes whose compatibility can be determined should be completed directly; uncertain business or compatibility issues pause only the related modifications, while independent work continues.** Do not automatically commit, merge, restore CI, or approve pending items on the user’s behalf.

### 0.4 Coverage requirements for large modules

The Agent breaks a module into sub-capabilities such as requests, storage, task lifecycle, and presentation, and assigns each file to a unit. Directory grouping is only for initialization and cannot replace item-by-item capability analysis.

Each unit must record: **search scope → candidate and caller analysis → rationale for modification or retention → verification results.** Coverage must include added files, deleted files, and changed files outside the module that are involved.

Tools will detect unassigned files, missing unit searches/analysis/verification, and files changed after review without re-review, among other issues; however, they cannot prove the Agent has fully understood the business. Ten or more changed source files (including deletions) automatically require coverage records, and smaller but complex modules should also enable them according to the rules.

### 0.5 Definition of done

| Completion condition | Result that must be checked |
| --- | --- |
| Complete scope | Task scope is clear; when module coverage is enabled, no units are missing and excluded files are explicitly listed |
| Sufficient implementation basis | Candidates, contract differences, callers, and handling approach have been recorded |
| Complete shared-capability registration | Capabilities that should be registered are in the catalog; candidates not registered have specific reasons |
| Verification matches the final code | Relevant checks pass; the report, task record, and verification logs match the current code |
| Blocking items handled | There are no related or unknown-scope pending issues; unfinished content is delivered truthfully |

> **A successful start, an exit code of 0, or the existence of a report file cannot by itself prove the task is complete.** Duplicate candidates still require analysis; when there are pending or unreviewed items, the report must state what remains unfinished.

### 0.6 Design boundaries

Skills define how the Agent should operate, and CLI validation checks deterministic evidence; there is currently no global edit interception and no separate workflow engine that forcibly executes each step. Records are auditable, but they do not authenticate operator identity and cannot prevent deliberate falsification.

For daily semantic search, the top six ranked files are inspected in detail by default; whole-repo review proceeds according to the full inventory, and coverage is not determined by those six results. Neither registration checks nor duplicate detection can guarantee exhaustive coverage of all business capabilities.

---

## 1. Execution flow and task scope

### 1.1 Unified execution flow

```text
User submits a task
    ↓
Record the task baseline; read rules, capability map, and applicable domain skills
    ↓
Determine file scope; split large modules into sub-capability units
    ↓
Search for existing implementations → read candidates, callers, and tests → decide implementation approach
    ├─ Plan is clear: modify code, migrate callers, register capability
    └─ Ambiguity exists: record the issue and ask; pause only modifications that depend on that decision
    ↓
Finish handling each unit and run relevant verification
    ↓
Check registration, coverage, duplicate candidates, and pending issues
    ↓
Confirm the report still matches the current code, then deliver changes and unfinished items
```

Existing modules can enter the review flow directly. The Agent analyzes the specified directory and its relation to other capabilities in the repository; it expands scope only when the user explicitly requests a whole-repo review.

### 1.2 Check scope

| Entry point | Duplicate-candidate check scope | Boundary of the check |
| --- | --- | --- |
| Ordinary task check | Source changes relative to the task start commit; at least one side of a candidate belongs to a changed file | Does not mean other modules have been fully reviewed |
| Whole-repo review task | All source code in scan scope, including duplicates already committed before the task started | Does not decide refactors automatically by similarity; does not require registering all historical exports |
| Service startup check | Primarily checks uncommitted source changes relative to current HEAD | Cannot cover all changes already committed during the task, nor rerun Agent analysis |

When a normal audit does not specify `--base`, it also detects duplicate candidates across all source code in the scan scope, but without the Agent’s per-module analysis and modifications, which is not equivalent to a full whole-repo review.

### 1.3 How tasks are initiated

For daily development, the user describes the requirement; module review must specify a directory and refactoring objective. Example instruction for a whole-repo task:

> Review the entire repository using the repo-review skill, complete all compatible refactors, registration, and tests that can be determined, and produce a final report.

The [repo-review skill](../../.agents/skills/repo-review/SKILL.md) is responsible for the full Agent execution flow. It delivers only after completing explicit modifications, and the user decides whether to keep them based on the report and code diff. Existing workspace changes must be preserved and must not all be attributed to the Agent.

Terminal commands can also prepare tasks, but **the CLI only generates inventories and instructions; it does not start AI**. Hand the generated instructions to the coding assistant, or have it continue according to the skills above. See Section 6 for commands.

## 2. Architecture and implementation responsibilities

### 2.1 Architecture selection rationale

Relying on conversational reminders alone cannot ensure consistent execution; an independent Agent service would add deployment and operations cost. Therefore, the design adopts “repository rules and skills + existing coding assistant + local CLI,” reusing Git, Node, TypeScript, and a local retrieval model.

```text
User requirement
   ↓
Coding assistant ← AGENTS.md + Workflow / UI / Backend / Deployment / Repo-review
   ├─ Reads source code, determines business contracts, modifies and verifies
   └─ Invokes reuse CLI
        ├─ Search: repository + search
        ├─ Registration: repository + registration
        ├─ Task evidence: evidence
        ├─ Pending items and approvals: decisions
        └─ Module coverage and reports: review
              ↓
       Code diffs + capability catalog + task/check reports
```

The local model is used only for retrieval; it does not mean the coding assistant’s large model also runs locally. No new remote embedding service or vector database is introduced.

### 2.2 Files and responsibilities

| Entry point or implementation | Responsibility |
| --- | --- |
| [AGENTS.md](../../AGENTS.md), [Workflow](../../.agents/skills/workflow/SKILL.md) | Route domain skills and define the workflow for discovery, reuse, registration, pausing, and verification |
| [UI](../../.agents/skills/ui/SKILL.md), [Backend](../../.agents/skills/backend/SKILL.md), [Deployment](../../.agents/skills/deployment/SKILL.md) | Constraints for components, interfaces, storage, and lifecycle in the corresponding domains; cross-domain tasks apply them together |
| [Repo-review](../../.agents/skills/repo-review/SKILL.md) | Item-by-item review, compatible refactoring, and delivery for whole-repo or existing large modules |
| [cli.mjs](../../tools/reuse/cli.mjs) | Shared command entry point that combines validation, retrieval, recording, and reporting |
| [repository.mjs](../../tools/reuse/repository.mjs) | Source discovery, declaration/snippet extraction, catalog validation, map generation, and duplicate detection |
| [search.mjs](../../tools/reuse/search.mjs) | Local model, vector cache, file and snippet ranking |
| [registration.mjs](../../tools/reuse/registration.mjs) | Detect shared declaration changes and relate them to registration or exclusion rationale |
| [evidence.mjs](../../tools/reuse/evidence.mjs) | Freeze task baselines, record file state, validate tests and report validity |
| [decisions.mjs](../../tools/reuse/decisions.mjs) | Issue records, evidence changes, impact scope, and real decision registration |
| [review.mjs](../../tools/reuse/review.mjs) | File inventory, unit coverage checks, Agent instructions, and Markdown reports |
| [dev.sh](../../scripts/dev.sh), [summary.mjs](../../tools/reuse/summary.mjs) | Invoke shared checks before startup; read valid reports and generate summaries |

Rules are maintained centrally under `.agents/skills/`; another parallel set of same-name rules is not maintained. Project instructions require the Agent to read applicable skills; whether the host auto-discovers them and when it loads them must be confirmed according to the actual environment. If they are not auto-discovered, ask the assistant to read the corresponding `SKILL.md`.

### 2.3 Directory structure and responsibility distribution

The following shows the Agent-related directories and major files; business source code and unrelated configuration are omitted. `.cache/reuse/` is a local artifact generated on demand and is not version-controlled.

```text
llm-d-prism/
├── AGENTS.md                          # Project rules: skill routing, reuse, registration, and pause requirements
│
├── .agents/skills/                    # Agent execution conventions
│   ├── workflow/SKILL.md              # Retrieval, technology selection, reuse analysis, registration, and verification
│   ├── repo-review/SKILL.md           # Whole-repo and large-module review, refactoring, and reporting
│   ├── ui/SKILL.md                    # React, Hooks, components, and browser clients
│   ├── backend/SKILL.md               # Node/Python APIs, storage, and task logic
│   └── deployment/SKILL.md            # Startup scripts, containers, cluster deployment, and CI
│
├── tools/reuse/                       # Local tool implementation and tests
│   ├── cli.mjs                        # Unified command entry point
│   ├── repository.mjs                 # File discovery, declaration extraction, map generation, and duplicate detection
│   ├── search.mjs                     # Semantic retrieval, vector cache, and candidate ranking
│   ├── registration.mjs               # Shared declaration changes and registration-candidate detection
│   ├── evidence.mjs                   # Task baselines, verification records, and report validity
│   ├── decisions.mjs                  # Pending issues, user decisions, and evidence changes
│   ├── review.mjs                     # Review inventory, unit coverage, and report generation
│   ├── summary.mjs                    # Check result summaries
│   ├── cli.test.mjs                   # Command and state tests
│   ├── repository.test.mjs            # Source analysis, catalog, and duplicate-detection tests
│   ├── search.test.mjs                # Retrieval and cache tests
│   ├── evidence.test.mjs              # Task evidence and report-validity tests
│   ├── dev-start.test.mjs             # Startup-flow integration tests
│   ├── semantic.smoke.mjs             # Real-model retrieval validation
│   └── retrieval-cases.json           # Retrieval regression samples
│
├── .reuse/                            # Long-term maintained data
│   ├── catalog.json                   # Capability usage, boundaries, source, examples, and tests
│   └── decisions.json                 # Pending issues and historical user decisions
│
├── docs/                              # Design and domain constraints
│   ├── reuse-map.md                   # Capability map automatically generated from the catalog
│   ├── storage-layout.md              # File storage, naming, and reference conventions
│   └── refactoring/
│       ├── README.md                  # Refactoring document index
│       └── reuse-first-agent-design.md # This design document
│
├── specs/changes/                     # Evidence and behavior acceptance specifications
│   ├── reuse-evidence.md              # Task evidence requirements
│   ├── reuse-module-review.md         # Module coverage and review requirements
│   └── reuse-agent-scenarios.md       # Actual Agent host acceptance scenarios
│
├── package.json                       # reuse-series npm commands
├── Makefile                           # test-js integration tool tests
├── scripts/dev.sh                     # Generates the map and runs supplemental checks before startup
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md       # PR reuse-analysis and verification notes
│   └── workflows/ci-pr-checks.yaml    # Standard CI, including tool tests
├── CONTRIBUTING.md                    # Development contribution conventions
├── .gitignore                         # Ignore rules for local cache and reports
│
└── .cache/reuse/                      # On-demand generated cache and delivery evidence
    ├── models/                        # Local retrieval model and tokenizer
    ├── index.json                     # File-summary and capability-vector cache
    ├── details.json                   # Vector cache for candidate code snippets
    ├── tasks/<task-name>.json         # Search, analysis, coverage, and verification records
    ├── <task-name>-instructions.md    # Review execution instructions
    ├── <task-name>-review.md          # Readable review report
    ├── <task-name>-review.json        # Review check data
    ├── report.json                    # Example path for ordinary task reports
    └── startup-report.json            # Startup check report
```

The execution relationship is: **project rules and Skills → coding assistant → local tools → capability catalog, task evidence, and reports**. The CLI `review` command prepares the inventory and instructions; the actual analysis and refactoring are performed by the coding assistant. Passing CI tool tests does not mean the PR reuse review has been completed.

## 3. Data model and recording responsibilities

### 3.1 Long-term capability catalog

[catalog.json](../../.reuse/catalog.json) stores reusable capabilities’ purpose, boundaries, source symbols, examples, and tests. [reuse-map.md](../reuse-map.md) is generated from it and serves as a retrieval entry point, not as a complete function inventory.

```json
{
  "version": 1,
  "entries": [{
    "id": "json-http",
    "path": "src/api/httpClient.js",
    "symbols": ["requestJson", "postJson", "readJson", "problemError"],
    "purpose": "JSON HTTP requests, response parsing, and unified API errors",
    "boundaries": "JSON only; streaming responses, binary downloads, and authentication strategy are handled by domain clients",
    "examples": ["src/components/AIProviders/aiProvidersBackend.js"],
    "tests": ["src/api/httpClient.test.js"]
  }]
}
```

The tools validate IDs, required fields, source and declarations, and example and test references. JS/TS uses the TypeScript syntax tree, and Python directory symbol validation uses Python AST. When there are no dedicated tests, fill `tests` with an empty array and use a real `testNote`.

The existence of references does not mean the contract or test content is correct; the Agent still needs to read them. After modifying the catalog, run `reuse:map`; do not edit the generated map directly. When a capability is migrated, deleted, or changed, maintain the entry and related references together.

### 3.2 Task evidence

Each task is saved as `.cache/reuse/tasks/<task-name>.json`.

| Content | Recording source and purpose |
| --- | --- |
| `taskId / baseCommit / initialFiles` | Created by the tool; freezes task identity, starting commit, and file snapshot |
| `applicableSkills / operations` | Filled by the Agent; explains applicable rules and concrete operations |
| `searches` | The tool appends queries, patterns, result paths, file states, and owning units |
| `analysis` | Filled by the Agent with candidates, handling approach, contract differences, and callers; module tasks associate them through `unitId` |
| `registrationReviews` | Specific exclusion reasons for unregistered candidates, bound to candidate file hashes |
| `verification` | Commands actually executed by the tool, exit codes, pre/post-execution state, logs, and log hashes |
| `coverage` | Module scope, initial inventory, exclusions, and handling status of each sub-capability unit |

The task baseline must be an ancestor of HEAD; a task spanning multiple commits continues to use the original baseline, and must not create a new task to hide already completed modifications. For normal audits, `--base REF` uses the merge-base of that reference and HEAD, and the report records both the requested and actual baselines.

### 3.3 Sub-capability coverage records

`coverage.units` is initialized by directory grouping. The Agent further splits it according to real responsibilities, ensuring every inventory file belongs to exactly one unit, while cross-unit relationships of shared files are written into the analysis.

| Unit content | Check requirement |
| --- | --- |
| File ownership | The original inventory, current inventory, and changed files outside scope involved in the task must all be assigned; deleted files still require review |
| Search and analysis | Must have searches with `--unit` and analysis with matching `unitId`; record callers, or explain with `callerNote` when there are none |
| `modified` | Explain the actual changes and reasons, and record the final file hash |
| `kept` | Explain why it was retained; it must not conflict with file changes after the starting snapshot |
| `blocked` | Link a real pending `decisionId`; the report must not mark the unit as reviewed-complete |
| Verification | Non-blocked units must have corresponding valid verification; the same test suite may be linked to multiple units |

`cover` records the current file hash, with null for deletions; this does not prove the Agent understood the file semantics correctly. Files must be re-reviewed after they change. Later-added `scope` uses the task-start snapshot; when an old task lacks that snapshot, baseline differences are conservatively included and noted in the report. Existing coverage cannot be reset through `scope`.

### 3.4 Manual decision ledger

[decisions.json](../../.reuse/decisions.json) records only issues that require human judgment; it does not carry ordinary reuse analysis.

| Field | Purpose |
| --- | --- |
| `id / kind / status` | Issue identity, category, and pending/resolved status |
| `summary / options` | The issue and at least two options |
| `evidence[].path / hash` | File evidence at the time of asking |
| `scope` | Optional explicit impact scope, including files, capabilities, callers, operations, and the source of scope analysis |
| `decision / approvedBy / approvalRef` | The actual decision, the decision-maker, and the conversation/review reference |
| `createdAt / resolvedAt / history` | Time and revision history |

Ordinary reuse judgments do not require item-by-item user approval. Only critical ambiguities that cannot be resolved from source, callers, and tests enter the ledger.

## 4. Check and pause rules

### 4.1 Missing-registration checks

Shared declaration candidates cover JS/TS explicit exports (including aliases, default exports, and re-exports), Python top-level non-private functions/classes, and also list related changes for already registered capabilities. They do not guarantee coverage of CommonJS dynamic exports or every Python dynamic public API.

In task mode, each candidate must either be linked to the catalog or have `{key, hash, reason}` filled in `registrationReviews`. Exclusion reasons are bound to the whole-file hash and must be re-reviewed after the file changes. The catalog should still record capabilities worth reusing, but does not require registering every function; registration candidates in whole-repo review are still generated from differences relative to the task baseline.

### 4.2 Conditions for pausing and continuing

| Issue category | What needs a user decision |
| --- | --- |
| `similarity` | Two similar code segments have different business behavior; whether to change or unify the contract |
| `semantic` | Multiple candidates are reasonable, but the requirement lacks selection criteria |
| `ci` | Whether to approve a specific scope exception to the rules |
| `index` | A capability’s ownership, documentation meaning, and source code conflict; whether it should be kept, migrated, or removed |

The Agent first completes safe analysis, providing locations, differences, callers, options, recommendations, and the paused operation, then executes `pause` and asks the question in conversation. Writing to the ledger does not mean the user has already been notified.

| Relationship between pending and the current task | Check behavior |
| --- | --- |
| Impact is explicit and the current files, related capabilities, or operations touch that scope | Blocking |
| Existing scope has been analyzed and the current task is outside it | Continue, but the report keeps the global pending item |
| No scope, referenced capability is unknown, or the original evidence has changed | Conservatively block; file-name differences alone are not enough to declare it unrelated |
| A review unit is explicitly marked blocked and references that issue | Blocking; do not count the unit as completed |

Scope judgment includes declaration files, evidence files, explicit callers, and entry/example/test references in the related catalog; changes to the definition of related capabilities also block. The tool does not prove the call-chain inventory is complete. Audits and startup have no task context, so they still conservatively handle all pending items.

After receiving a real user decision, record it first and then update evidence files. If evidence has already changed before the reply, use `pause --id` to revise it while preserving history, and present the issue again rather than reusing the old answer. `resolve` rejects records missing approval fields or with mismatched evidence, and never infers approval from silence or time.

### 4.3 Scope of historical decisions

When the content tied to a resolved record changes or a file is missing, it is listed in `staleDecisionIds`, with `content-changed` or `missing-or-unsafe` explained in `staleDecisions`.

This does not revoke the historical decision or independently block the task. For example, if the user approved deleting a file, the fact that the evidence file is missing may be exactly the expected result. But old records do not provide permanent authorization for new operations, and old hashes must not be rewritten to suppress the warning.

### 4.4 When verification results become invalid

`verify` executes argv directly without going through a shell, saving the exit code and logs. For the same command, the most recent result wins; an earlier success cannot override a later failure. When checking verification, the system compares the pre/post-execution state and log hashes.

Report v2 binds Git-visible file contents, execute permissions, add/delete status, and HEAD, covering code, tests, configuration, and documentation. Task JSON is separately bound by `task.hash`. Missing or changed verification logs also invalidate the report.

`.cache/reuse/` is excluded to avoid self-reference; Git-ignored runtime data is outside the guarantee scope. These are locally editable audit materials and do not defend against malicious tampering. Old reports without valid metadata are also rejected.

### 4.5 Check states and exit codes

| State | Meaning | Exit code |
| --- | --- | --- |
| `INVALID` | Deterministic errors exist in registration, coverage, task evidence, verification, etc. | 1 |
| `WAITING_FOR_USER` | No such errors, but there are blocking pending items | 2 |
| `CANDIDATES_REQUIRE_ANALYSIS` | Duplicate candidates exist and require contract analysis by reading | 0 |
| `REGISTRATION_REVIEW_REQUIRED` | Audit/startup found unhandled registration candidates; in task mode, unhandled candidates make the result INVALID | 0 |
| `CHECKED` | None of the above items exist within the current tool check scope | 0 |

State priority is: errors, blocking pending items, duplicate candidates, registration candidates, CHECKED; the full report retains each list. Exit code 0 does not prove the business judgment is correct.

Module/whole-repo Markdown reports use REVIEWED or INCOMPLETE to indicate completion level. If there are coverage errors, blocked units, or pending items, the report must not be marked complete. When checks fail, the status is refreshed to incomplete to avoid stale successful reports lingering; parameter/path parsing failures, startup map-stage failures, and similar issues still need to be interpreted together with terminal output.

## 5. Retrieval algorithm and scan boundaries

### 5.1 Candidate retrieval scope

`discover()` uses Git to obtain tracked files and new non-ignored files, then filters them by the following scope:

| Dimension | Scope |
| --- | --- |
| Directories | `src/`, `server/`, `llm_d_bench/`, `tools/`, `scripts/` |
| File types | JS, JSX, TS, TSX, MJS, CJS, Python |
| Exclusions | Tests, declaration files, dependencies, build artifacts, some generated files, invalid or unsafe paths |
| Special exclusion | Auto-generated `server/mcp/tools.ts` |

Other file types such as Shell and SQL do not currently participate in semantic or similar-snippet scanning. Test files are not treated as implementation candidates, but the AI must still read relevant tests as contract evidence.

### 5.2 Two-stage retrieval and default recall

```text
Source files → file summaries ─────────────┐
catalog → standalone capability-purpose descriptions ─────┤
                                ▼
                      Local model generates vectors
                                │
Requirement description → query vector → cosine similarity ranking
                                │
                 Select top N distinct files (default 6)
                                │
            40-line overlapping code snippets → rerank
                                │
            Return capabilities / files / results
```

Implementation parameters: multilingual MiniLM, fixed model revision, q8, CPU, mean pooling, vector normalization, input limit 256 tokens. File summaries include directory description, path, symbols, and source header; capability-purpose descriptions are vectorized separately so implementation details do not drown out purpose descriptions.

Code snippets start from function/class entry points and every 30 lines thereafter, with each segment up to 40 lines; there is a separate body-length limit for content entering snippet retrieval. The ranking is a two-stage candidate retrieval process, not an exhaustive semantic proof over all functions.

| Cache | Content | Update method |
| --- | --- | --- |
| `.cache/reuse/models/` | Fixed-version model and tokenizer | Downloaded on first use, then can be loaded offline |
| `.cache/reuse/index.json` | File-summary and capability-description vectors | Incrementally updated during search according to text hash and model configuration |
| `.cache/reuse/details.json` | Code-snippet vectors for the currently selected files | Updated with candidate files and snippet content |

The model is `Xenova/paraphrase-multilingual-MiniLM-L12-v2`, pinned to revision
`2c4055b12046f11709e9df2c122e59ffbdc2f900`. The first search or `reuse:index` downloads the public model,
and afterward `--offline` can be used. Retrieval runs locally and does not upload source code; models, caches, and reports are not committed.
Search incrementally updates the index and removes deleted entries; starting services does not download the model or update vectors.

If the model is unavailable, the command fails explicitly. `--lexical` is a keyword-based fallback path explicitly selected by the user or AI; results are marked `lexical-not-semantic` and are not disguised as successful semantic search.

### 5.3 Duplicate-candidate detection mechanism

`duplicates()` performs lexical scanning, removes some comments, normalizes non-keyword identifiers and literals, and compares contiguous 70-token windows across files within the same language family. For each file pair, one group of candidate positions is retained for analysis.

When an ordinary check specifies `--base`, the merge-base of that reference and HEAD is used as the baseline, and compared against the full current workspace; at least one side of a candidate must belong to a changed file. Whole-repo review tasks still scan duplicate candidates across all source code. Detection targets changed files and is not strictly limited to newly added lines, so it may report pre-existing duplicates within those files.

The output includes candidate ID, paths and start/end lines on both sides, window length, and hints. It currently does not cover same-file clones, cross-language clones, arbitrary statement reordering, or all equivalence relationships in complex syntax. Candidate counts must not be used directly as a code-quality score.

### 5.4 Recall parameters and degradation

`--file-limit` defaults to 6, with a range of 1–100, controlling how many distinct files enter detailed retrieval; `--limit` controls the number of returned snippets. Capability-purpose descriptions are also ranked independently. Increasing the file count can help recall, but cannot replace caller analysis.

`--lexical` scans all candidate source snippets and matches substrings after whitespace tokenization; it does not perform full Chinese tokenization or synonym retrieval, and `file-limit` does not restrict its scan scope. The Agent must clearly state that lexical mode was used and supplement it with `rg` and similar tools; zero results cannot prove that no reusable implementation exists in the repository.

### 5.5 Whole-repo inventory and retrieval scope

The coverage inventory includes source code, tests, Shell scripts, configuration, and documentation in Git-visible text. Dependencies, generated artifacts, symlinks, binaries, and files larger than 1 MiB are explicitly excluded; Git-ignored files are not part of the inventory.

Therefore, “whole-repo file coverage” and “duplicate-candidate scanning” are not the same scope. Excluded files must not be treated as already reviewed, and large text requires separate analysis arrangements. The top six files only serve each sub-capability search and do not limit the overall file inventory.

## 6. Entry points and report locations

The following commands are for the Agent to execute or for developers to troubleshoot; they do not require the user to run them manually every time. The environment requires Node 22, Python 3, Git, and installed npm dependencies; examples are run at the repository root. Task names are defined by the user or Agent, and a new task must not overwrite an existing same-name record.

### 6.1 Ordinary development tasks

```bash
npm run reuse -- begin --task feature-work
npm run reuse:search -- "behavior to implement" --task feature-work
# The Agent fills in skills, operation scope, and candidate analysis, then completes the implementation
npm run reuse:check -- --base TASK_START_SHA --json
# Handle registration candidates and update the catalog
npm run reuse:map
npm run reuse -- verify --task feature-work -- npm run reuse:test
npm run reuse:check -- --task feature-work --base TASK_START_SHA --report .cache/reuse/report.json
npm run reuse -- verify-report --report .cache/reuse/report.json
```

Replace `TASK_START_SHA` with the actual task start commit, and replace or supplement `reuse:test` with the real regression tests for the relevant domain. Example analysis entry for an ordinary task:

```json
{
  "capability": "JSON requests",
  "candidates": ["src/api/httpClient.js"],
  "selectedApproach": "reuse",
  "rationale": "The existing JSON response and error contract is applicable, and the page keeps its own state management"
}
```

Allowed values for `selectedApproach` are reuse, adapt, extend, extract, and new. Search and test execution results are appended by the tool and must not be fabricated manually.

### 6.2 Module review

```bash
npm run reuse -- begin --task evaluation-review --module src/components/evaluation
# Use scope to supplement coverage for an existing task, rather than begin again
npm run reuse -- scope --task existing-task --module src/components/evaluation
```

Choose initialization or supplemental coverage according to task status; for multiple directories, repeat `--module`. After the Agent splits the inventory into units, process them one by one:

```bash
npm run reuse:search -- "specific behavior of this sub-capability" --task evaluation-review --unit unit-1
# Add unitId and callers or callerNote to analysis, then complete the analysis and explicit modifications
npm run reuse -- cover --task evaluation-review --unit unit-1 --outcome modified --summary "specific changes and reasons"
npm run reuse -- verify --task evaluation-review --unit unit-1 -- npm run reuse:test
npm run reuse -- review-report --task evaluation-review
npm run reuse -- verify-report --report .cache/reuse/evaluation-review-review.json
```

`cover` can also use kept; blocked requires `--id` to link a real pending item. When the same test covers multiple units, repeat `--unit`, and reverify against the final code state. Directories/maps, documentation, configuration, and affected out-of-scope callers should all be assigned to a unit.

### 6.3 Whole-repo review initialization

```bash
npm run reuse:review -- --task repo-audit
```

When only a module is specified:

```bash
npm run reuse:review -- --task evaluation-audit --module src/components/evaluation
```

`--task` specifies the task name, and `--module` specifies the module scope; the first `--` after npm is used to pass arguments through. The command does not start the Agent by itself. The coding assistant reads `.cache/reuse/<task-name>-instructions.md` and continues according to the repo-review skill.

After the Agent finishes item-by-item processing, execute:

```bash
npm run reuse -- review-report --task repo-audit
npm run reuse -- verify-report --report .cache/reuse/repo-audit-review.json
```

### 6.4 Pause and decision registration

```bash
npm run reuse -- pause --kind similarity \
  --summary "An existing operation retries, but whether the new requirement may retry is not yet clear" \
  --file src/api/httpClient.js \
  --option "Keep existing behavior and adapt the caller" \
  --option "Change the shared contract and migrate callers"
```

When there is an explicit impact scope, `--scope .cache/reuse/scope.json` may be added. The scope file contains `files`, `callers`, `capabilityIds`, and `operations` arrays, plus `rationale`, `reviewedBy`, and `reviewRef`. The latter two record the true source of the scope analysis, not business approval; when the impact scope is unknown, do not invent a scope to bypass blocking.

After receiving the actual decision:

```bash
npm run reuse -- resolve --id REQUEST_ID \
  --decision "actual decision and approved scope" \
  --approved-by "actual decision maker" \
  --approval-ref "actual conversation or review reference"
```

The above is only an example of parameter format; identity and approval information must come from a real user decision. When necessary, revise pending evidence with `pause --id REQUEST_ID` while preserving history.

### 6.5 Reports and maintenance commands

| Artifact | Path | Purpose |
| --- | --- | --- |
| Task evidence | `.cache/reuse/tasks/<task-name>.json` | Search, analysis, coverage, and test records |
| Agent execution instructions | `.cache/reuse/<task-name>-instructions.md` | Execution instructions generated by the review command |
| Readable review report | `.cache/reuse/<task-name>-review.md` | Modifications, retained parts, callers, verification, pending items, and exclusions |
| Review check data | `.cache/reuse/<task-name>-review.json` | Verified for validity using verify-report |
| Ordinary task check report | Path specified by `--report` | For example `.cache/reuse/report.json` |
| Startup check report | `.cache/reuse/startup-report.json` | Indicates startup-time check results only |

Reports show changes after the task snapshot and all changes relative to the commit baseline. If an old task lacks an initial snapshot, the report explicitly states that it inferred from the baseline, and must not attribute all existing workspace changes to the current modification.

Reports, tasks, and logs are Git-ignored by default and should be exported together during review; local paths are not permanent shared links. `check --report` requires the JSON file to be under `.cache/reuse/` and not inside `tasks/`, to avoid overwriting source code or task input.

| Maintenance purpose | Command |
| --- | --- |
| Update the capability map | `npm run reuse:map` |
| Manually check whole-source candidates without running the Agent | `npm run reuse:check` |
| Explicit lexical retrieval | `npm run reuse:search -- requestJson --lexical` |
| Offline retrieval with an already cached model | `npm run reuse:search -- "retry after load failure" --offline` |
| Build the semantic index | `npm run reuse:index` |
| Tool regression | `npm run reuse:test` |
| Real-model retrieval regression | `npm run reuse:test-model` |

## 7. Startup, CI, and verification boundaries

### 7.1 Supplemental checks before startup

```text
prepare_start
  → check environment
  → cli.mjs map
  → cli.mjs check --purpose startup --base HEAD
       --report .cache/reuse/startup-report.json

start:   prepare_start → start services
restart: prepare_start → stop services → start services
```

If map generation or deterministic checks fail, the process returns an error before stopping services. When pending returns 2, it reminds the operator and continues startup; the ledger remains pending and does not approve the related refactor.

Startup does not download retrieval models, update vectors, or rerun Agent business analysis. `--base HEAD` mainly covers uncommitted source changes and cannot replace checks against the original task start commit. If the map stage fails, an old startup report may still exist and must be interpreted together with terminal output.

### 7.2 Commits and CI

There are currently no new commit hooks and no separate automatic PR reuse review. In the existing `ci-pr-checks.yaml`, `make test-js` includes `reuse:test`, which validates the tool itself rather than whether each PR correctly completed reuse analysis.

The historical ledger records an explicit decision to delete a dedicated CI workflow. Restoring it in the future would require a new explicit user decision and confirmation of runner network conditions; it should reuse the current CLI and configure the correct baseline, rather than using another implementation or treating similarity candidates directly as failures to bypass the decision.

### 7.3 Verification scope and limitations

| Verification method | What it verifies | Outside the guarantee scope |
| --- | --- | --- |
| Skill discovery and reading | The current host can find the rules | Every task will necessarily follow the rules completely |
| Tool unit/integration tests | Covered catalog, decisions, tasks, reports, and startup boundaries | All business interfaces are correct |
| Real-model samples | The specified queries can find the target; sample Recall@6/12 | Any requirement can find the most suitable implementation |
| Module coverage checks | Inventory files and declaration units have corresponding evidence | Business sub-capability splitting is absolutely complete, or tests are definitely sufficient |
| Actual Agent rehearsal | Retrieval, modification, registration, and verification behavior in this rehearsal | All future tasks and hosts will operate correctly |
| Report freshness checks | The report still matches the current visible inputs and verification logs | All manual judgments in it are correct, or the service is healthy |

Real retrieval samples are in [retrieval-cases.json](../../tools/reuse/retrieval-cases.json), and Agent rehearsal uses the [behavior acceptance protocol](../../specs/changes/reuse-agent-scenarios.md). Keep real tool traces; explicitly mark scenarios not executed, and do not substitute tool tests for Agent behavior verification.

### 7.4 Maintenance principles

When capabilities change, maintain the catalog, examples, tests, and retrieval samples together; if the model or cache fails, raise a clear error rather than explaining the failure as “no reusable code exists.” Status and pass counts are based on the current run and are not maintained as fixed numbers in the design document.

Documentation adjustments require only reference and consistency checks; do not download models or restart services just for text edits. The design document cannot replace a delivery report generated from the current code state.

Related entry points: [Workflow](../../.agents/skills/workflow/SKILL.md) · [Repo-review](../../.agents/skills/repo-review/SKILL.md) · [Capability map](../reuse-map.md) · [Shared Component Inventory and File Conventions](shared-code-guide.md) · [Storage conventions](../design/storage-layout.md).
