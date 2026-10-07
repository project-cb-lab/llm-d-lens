# Nine Shared Capabilities Integration Record

This round completed the nine shared capabilities confirmed by the previous search round and migrated the callers listed in the table. The scope does not include structured data storage, DB type ownership, or monitoring business-flow refactoring; this is not a declaration that “the entire repository is now free of duplicate code.”

| Item | Shared entry point | Integrated locations | Preserved boundary |
| --- | --- | --- | --- |
| Time formatting | [formatTimestamp](../../src/utils/formatTimestamp.js) | SimulationDashboard; original deployment/storage callers continue to use it | Null handling, invalid-date fallback, and browser time zone remain unchanged |
| Search debounce | [useDebouncedValue](../../src/hooks/useDebouncedValue.js) | StorageManagementPage, ModelCachePage, DeploymentManagementPage | 300ms; each page remains responsible for trim, filtering, and request cancellation |
| Auto-dismiss notices | [useNotice](../../src/hooks/useNotice.js) | The three resource pages above | 4 seconds; clears old timers when notices change or on unmount, and same-value updates keep original useState behavior |
| Pagination calculation | [paginate](../../src/utils/pagination.js) | The three resource pages above, together with existing PaginationControls | Only responsible for page count, valid page number, display range, and slicing; pages keep filtering, sorting, and reset conditions |
| Browser file download | [downloadBlob / downloadFile](../../src/utils/download.js) | Download entry points in resultExports, OptimizationEvaluationDetails, and SimulationDashboard | Filename, authentication, and file-type validation remain at the original layer; Blob URLs are released after a 1-second delay, and temporary links are removed |
| Copy and success feedback | [copyText](../../src/utils/clipboard.js), [useClipboard](../../src/hooks/useClipboard.js) | DeploymentTable, DeploymentDetailsModal, DeploymentPodsLogsModal, OptimizationClusterOverview, OptimizationWorkspace, ShareLinkButton | Preserves 1.5/2-second feedback duration; supports HTTP fallback; failures do not show success; old async results and post-unmount results do not update state |
| Namespace validation | [validate_namespace](../../llm_d_bench/utils/kubernetes.py) | accelerator/service.py, cluster_stack/service.py | Service-layer compatibility export preserved; original regex, 63-character limit, and ValueError copy remain unchanged |
| Node request timeout and parsing | [fetchJsonWithTimeout](../../server/http.ts) | configuration.ts, candidateSearch.ts | Timeout covers response-body reading; timers are cleaned up; empty response becomes `{}`, non-JSON becomes detail; status-code and error mapping remain caller decisions |
| Byte conversion | [scaleBytes](../../src/utils/formatBytes.js) | SimulationDashboard, OptimizationClusterOverview | Simulation keeps GiB upper bound and one decimal place; overview keeps PiB upper bound and omits decimals for integers; null-value strategies are preserved separately |

## Verification

| Check | Result |
| --- | --- |
| `make test-js` | 312 frontend and Node tests passed; 21 reuse-tool tests passed. Then 8 more tests passed in total for added route-contract tests and related shared-function tests |
| Python namespace and tests for the two monitoring domains | 47 tests passed |
| `npm run build` | Passed; existing large-bundle warning remains |
| `npm run type-check` | Passed |
| React StrictMode browser verification | Debounce replacement, notice replacement/expiration, consecutive copy timers, and unmount before copy completion all passed; no pageerror |
| ESLint | Existing 29 errors / 10 warnings remain, so whole-repo lint cannot be described as passing |
| Python Ruff (related four files) | 9 existing rule issues; see the log below for details. ESLint checks for newly added code in this round passed |
| Reuse checks | `errors: []`; status remains `WAITING_FOR_USER` because the earlier DB ownership issue for PageInfo / TaskRef / TaskStatus remains unresolved and does not depend on these nine items |

Local verification logs are located at `/tmp/nine-js.log`, `/tmp/nine-python.log`, `/tmp/nine-final-targeted.log`, `/tmp/nine-build.log`, `/tmp/nine-types-final.log`, `/tmp/nine-browser.log`, `/tmp/nine-lint.log`, and `/tmp/nine-ruff.log`. The temporary browser verification script is `/tmp/nine-hooks-browser.mjs`, using Playwright already installed on this machine; it is not part of the repository’s automated test suite. No real cluster or external provider was called.

## Reuse report review

Full report: `.cache/reuse/nine-capabilities-final.json`. The final report for this round contains 80 candidate pairs, and the full list together with non-trivial matching source code was reviewed.

- Store candidates belong to the file-storage implementation that the user had already required to roll back, so they remain unchanged.
- Many matches are imports, useState declarations, and object literals in different business forms; lifecycle logic is not merged on that basis.
- Delete/submit dialogs already compose Modal, Button, and useSubmission; domain confirmation steps remain separate.
- Monitoring links, status mapping, search-bar appearance, and similar items still have candidates beyond these nine items, and cannot be written as already completed; this round is not expanded into monitoring-domain or page-style refactoring.
- Pagination size selection remains a page policy; the shared paginate does not handle server-side pagination or total request counts.
- ModelCache’s pendingSyncNodes and Storage’s nodesAdded have different meanings, so each keeps its own business logic.

The capabilities have been registered in [.reuse/catalog.json](../../.reuse/catalog.json), and the [capability index](../reuse-map.md) has been regenerated. Existing pending confirmation records were neither modified nor lifted.
