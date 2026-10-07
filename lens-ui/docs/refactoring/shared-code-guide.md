# Shared Component Inventory and File Conventions

## 1. Shared capabilities already extracted and integrated uniformly

### Frontend components and Hooks

| Shared capability | File path | Extracted content | Integration scope |
| --- | --- | --- | --- |
| Async state display | [src/components/shared/AsyncState.jsx](../../src/components/shared/AsyncState.jsx) | Loading, error retry, empty state, and content display | AI Provider, Storage, Model Cache, Deployment |
| Form error display | [src/components/ui/FormError.jsx](../../src/components/ui/FormError.jsx) | Error prompt styling and accessibility markers | Create, edit, delete, and log dialogs |
| Pagination controls | [src/components/ui/PaginationControls.jsx](../../src/components/ui/PaginationControls.jsx) | Page-turning buttons, page numbers, and disabled states | Storage, Model Cache, Deployment |
| Submission state | [src/hooks/useSubmission.js](../../src/hooks/useSubmission.js) | In-submission state, error state, and duplicate-submission protection | Resource forms and delete dialogs |
| Search debounce | [src/hooks/useDebouncedValue.js](../../src/hooks/useDebouncedValue.js) | Delayed input-value updates and timer cleanup | Storage, Model Cache, Deployment |
| Temporary notices | [src/hooks/useNotice.js](../../src/hooks/useNotice.js) | Notice state and automatic clearing | Storage, Model Cache, Deployment |
| Copy feedback | [src/hooks/useClipboard.js](../../src/hooks/useClipboard.js) | Copy success state, feedback timing, and unmount cleanup | Deployment table, details, logs, cluster overview, configuration cards, share button |

### Frontend shared method reuse

| Shared capability | File path | Main methods | Extracted content |
| --- | --- | --- | --- |
| JSON requests | [src/api/httpClient.js](../../src/api/httpClient.js) | `requestJson`, `postJson`, `readJson`, `problemError` | Request sending, response parsing, and foundational error handling |
| Monitoring request adapter | [src/api/monitoringClient.js](../../src/api/monitoringClient.js) | `requestMonitoringJson` | Response fallback and error-code adaptation |
| Submission concurrency protection | [src/utils/submissionGuard.js](../../src/utils/submissionGuard.js) | `createSubmissionGuard` | Synchronous duplicate-submission blocking, failure unlock, and success lock |
| Error copy | [src/utils/errorMessage.js](../../src/utils/errorMessage.js) | `errorMessage` | Unified selection order for error details, messages, and default copy |
| Time formatting | [src/utils/formatTimestamp.js](../../src/utils/formatTimestamp.js) | `formatTimestamp` | Local-time display and invalid-value fallback |
| Byte conversion | [src/utils/formatBytes.js](../../src/utils/formatBytes.js) | `scaleBytes` | Binary unit conversion; precision and unit upper bound are configurable |
| Pagination calculation | [src/utils/pagination.js](../../src/utils/pagination.js) | `paginate` | Page count, valid page number, display range, and array slicing |
| File download | [src/utils/download.js](../../src/utils/download.js) | `downloadBlob`, `downloadFile` | Download-link creation, Blob URL release, and remote file download |
| Text copy | [src/utils/clipboard.js](../../src/utils/clipboard.js) | `copyText` | Clipboard API, HTTP-environment fallback, and temporary-element cleanup |
| Configuration checksum | [src/features/evaluation/checksum.js](../../src/features/evaluation/checksum.js) | `configurationChecksum`, `sha256` | Configuration normalization and SHA-256 calculation |
| Cluster session | [src/components/OptimizationWorkspace/clusterBackend.js](../../src/components/OptimizationWorkspace/clusterBackend.js) | `openClusterSession` | Session creation and returned-ID normalization |
| Software download polling | [src/components/OptimizationWorkspace/clusterBackend.js](../../src/components/OptimizationWorkspace/clusterBackend.js) | `waitForSoftwareDownloads` | Shared download-status polling for create/edit cluster |

### Backend common functionality

| Shared capability | File path | Main entry point | Extracted content |
| --- | --- | --- | --- |
| Node upstream requests | [server/http.ts](../../server/http.ts) | `fetchJsonWithTimeout` | Request timeout, response-body reading, and JSON parsing; business error mapping remains in the route layer |
| Exceptions | [llm_d_bench/core/exceptions.py](../../llm_d_bench/core/exceptions.py) | `DomainError`, `NotFoundError`, `ConflictError`, `DomainValidationError` | Shared exception categories and foundational status codes |
| API exception adaptation | [llm_d_bench/api/problems.py](../../llm_d_bench/api/problems.py) | `install_problem_handlers` | Response conversion for domain exceptions, HTTP exceptions, and request-validation errors |
| Namespace validation | [llm_d_bench/utils/kubernetes.py](../../llm_d_bench/utils/kubernetes.py) | `validate_namespace` | Name format, length, and error copy shared by the two monitoring modules |

### Reuse of existing implementations

| Shared capability | File path | Purpose |
| --- | --- | --- |
| Foundational UI | [src/components/ui/](../../src/components/ui) | Foundational components such as Button, Modal, FormControls, and EmptyState |
| Chart foundation layer | [src/components/ui/charts/](../../src/components/ui/charts) | Chart containers, legends, tooltips, and color schemes |
| Resource loading | [src/utils/resourceLoader.js](../../src/utils/resourceLoader.js) | Caching and in-flight request reuse |
| Evaluation configuration validation | [src/features/evaluation/configurationValidation.js](../../src/features/evaluation/configurationValidation.js) | Evaluation input validation and server-side error mapping |
| Temporary directories | [llm_d_bench/utils/paths.py](../../llm_d_bench/utils/paths.py) | Temporary root directory and environment-variable configuration |

Structured-data Store extraction has been rolled back and will be managed by the DB PR; `PageInfo`, `TaskRef`, and `TaskStatus` are not counted within the scope of shared capabilities already integrated.

## 2. Shared file placement and naming conventions

The naming constraints are maintained together in the [UI skill](../../.agents/skills/ui/SKILL.md) and the [backend skill](../../.agents/skills/backend/SKILL.md); the [workflow skill](../../.agents/skills/workflow/SKILL.md) requires checking before creation and before final delivery.

### Frontend

| Type | Directory | File naming | Export naming | Example |
| --- | --- | --- | --- | --- |
| Foundational UI components | `src/components/ui/` | `PascalCase.jsx` | PascalCase, matching the component name | `PaginationControls.jsx` → `PaginationControls` |
| Chart components | `src/components/ui/charts/` | `PascalCase.jsx` | PascalCase | `ChartTooltip.jsx` → `ChartTooltip` |
| Shared composed components | `src/components/shared/` | `PascalCase.jsx` | PascalCase | `AsyncState.jsx` → `AsyncState` |
| React Hook | `src/hooks/` | `usePascalCase.js` | `use` + PascalCase | `useClipboard.js` → `useClipboard` |
| Shared utility functions | `src/utils/` | `camelCase.js`, named by function | camelCase; use verbs for action methods | `pagination.js` → `paginate` |
| Browser capabilities | `src/utils/` | `camelCase.js`, named by function | camelCase | `clipboard.js` → `copyText` |
| HTTP clients | `src/api/` | `<scope>Client.js` | camelCase | `httpClient.js` → `requestJson` |
| Domain shared logic | `src/features/<domain>/` | `camelCase.js`, named by responsibility | camelCase | `evaluation/checksum.js` → `configurationChecksum` |
| Domain UI components | `src/components/<Domain>/` | `PascalCase.jsx` | PascalCase | `DeploymentTable.jsx` → `DeploymentTable` |

Existing `*Backend.js` domain clients continue to be extended in their original directories; new standalone domain logic uses `src/features/<domain>/`. UI components are responsible for presentation, Hooks are responsible for React state, utils do not depend on React, and domain rules remain in domain modules.

### Node and Python

| Type | Directory | File naming | Export naming | Example |
| --- | --- | --- | --- | --- |
| Node shared capabilities | `server/` | `camelCase.ts`, named by responsibility | Functions in camelCase, types in PascalCase | `http.ts` → `fetchJsonWithTimeout` |
| Node domain modules | `server/` or an existing domain subdirectory | `<domain><Responsibility>.ts` | Functions in camelCase, types in PascalCase | `clusterSources.ts` → `loadClusterSources` |
| MCP adapters | `server/mcp/` | `camelCase.ts` | Functions in camelCase | `specialTools.ts` |
| Python shared foundational types | `llm_d_bench/core/` | `snake_case.py` | Classes in PascalCase | `exceptions.py` → `DomainError` |
| Python HTTP adapters | `llm_d_bench/api/` | `snake_case.py` | Functions in snake_case | `problems.py` → `install_problem_handlers` |
| Python technical utilities | `llm_d_bench/utils/` | `snake_case.py`, named by function | Functions in snake_case, classes in PascalCase | `kubernetes.py` → `validate_namespace` |
| Python domain logic | `llm_d_bench/<domain>/` | `snake_case.py`, named by responsibility | Functions in snake_case, classes in PascalCase | `configuration/normalizers.py` |
| Python domain models | `llm_d_bench/<domain>/` | `models.py` or an existing model file | PascalCase | Domain request, response, and data models |
| Data persistence | The data-access directory determined by the DB PR | Follow the DB PR conventions | Follow the DB PR conventions | Do not add a shared file Store layer |

### Tests

| File type | Location | Naming format | Example |
| --- | --- | --- | --- |
| JS / TS tests | Same directory as the file under test | `<filename>.test.js` / `<filename>.test.ts` | `http.test.ts` |
| JSX tests | Same directory as the component under test | `<Component>.test.jsx` | `PaginationControls.test.jsx` |
| Python tests | Relevant domain or utility directory | `test_<capability>.py` | `test_namespace.py` |
| Shared capability registration | `.reuse/catalog.json` | IDs use `kebab-case` | `clipboard-feedback` |
| Generated capability index | `docs/reuse-map.md` | Fixed filename | Generated by `npm run reuse:map` |
