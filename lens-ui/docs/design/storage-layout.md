# Lens Unstructured Data Storage Scheme

This scheme unifies the storage location, directory naming, and reference method for files generated or downloaded during Lens operation, such as logs, charts, source code, and model weights. The rules apply to both existing features and new features.

## 1. Original Storage Locations and Data Scale

The scope covers unstructured content and its related configuration, task records, and credentials. Logs, conversations, audio, images, and model weights are unstructured content; JSON/YAML configuration, task records, and CSV metrics are structured or semi-structured data. A total of 16 categories are listed by purpose.

### Data-generation Stages and Original Paths

`C=~/.cache/llm-d-bench`, `D=~/.llm-d-bench`, and `~` is the home directory of the user running the service. The following are the original default paths; custom deployments use their own configuration.

| Function and content | Generation stage / trigger | Storage method and location |
| --- | --- | --- |
| Configuration files and configuration snapshots | When editing is complete and the configuration is saved | YAML under `C/configurations/<filename>`; JSON snapshots under `.artifacts/<configurationID>.json` |
| Deployment manifests and temporary configuration required for deployment | During deployment preparation and Kubernetes configuration generation | `C/deployment-manifests/`, `C/tmp/`; some files are inside the downloaded llm-d source directory |
| Deployment logs and Kubernetes events | During deployment execution, readiness checks, and failure diagnosis | `D/deploy/logs/<executionID>/entries.jsonl`, storing collected log fragments |
| Deployment tasks and execution states | When deployments are created and state changes | `D/deploy/{runs,executions}/`, one JSON file per record |
| Evaluation inputs, raw logs, metrics, and charts | At evaluation start, warm-up, runs for each parameter group, and result aggregation | `C/prism-evaluate-results/<runID>/`; contains subdirectories such as `warmup/` and `matrix-<index>/` |
| Evaluation workflows, run records, and log tails | When evaluation tasks are created and during execution | `~/.llm_d_bench/run_store/evaluate/{runs,workflows}/`; old development scripts use `private/run_store/evaluate/` inside the repo |
| Simulation tasks, request details, summaries, and process logs | When simulation tasks are created, requests are replayed, and results are organized | `C/simulations/<taskID>/task.json` and `artifacts/<runner tool>/` |
| Trace request datasets and indexes | When datasets are downloaded or generated and replay is prepared | Datasets and timeline indexes in `C/datasets/`; converted request files in the simulation task directory |
| KV cache access records | Generated with inference requests when collection is enabled and aggregated at evaluation end | `/tmp/prism-kv-trace/` in the inference-process environment; aggregated files enter the evaluation result directory |
| Cluster configuration and connection files | When clusters are added, kubeconfig is uploaded, and connections are opened | `C/data/cluster/{clusters,cluster_sessions}/` |
| Cluster installation files | When clusters are created and Kubespray is executed | `C/data/cluster/bootstrap/<installationTaskID>/`, including machine inventory, installation parameters, and management credentials; temporary keys are cleaned up after completion |
| Monitoring component installation logs | When monitoring components are installed or updated | `D/monitoring/operations/*.json`, with logs included in operation records |
| Model weights, tokenizers, and model configuration | When a model download is initiated and the cluster download task runs | Actual files are on the selected cluster storage volume, mounted inside the container as `/model-cache`; registration records are in `D/model-cache/entries/` |
| Software repositories and runner tool caches | On first use of tools, repository download, and runtime-environment preparation | `C/{repos,backends,tokenizers,kubespray}/`; there are also Guide caches in `~/.tmp/` and the system temp directory |
| Assistant conversations, tool-return text, and recordings | During chat, tool invocation, and recording | Conversations and recordings are mainly in browser memory; speech-recognition models are in browser cache; no unified server-side chat archive was found |
| Exported reports, service logs, and development retrieval files | When Export is clicked, services are started, and development retrieval tools are run | Reports go to the browser download location; old service logs are in `.dev-logs/` and `.prism-logs/`; retrieval files are in `.cache/reuse/` |

The original paths for storage-volume registration, AI Provider configuration, and Agentic run registration are `C/storage/`, `C/ai-providers/`, and `D/agentic/benchmarks/` respectively.

### Local Inventory Snapshot

| Existing directory | File count | Approx. size |
| --- | ---: | ---: |
| `C/prism-evaluate-results/` | 19,997 | 145.00 MiB |
| `D/deploy/` | 84 | 14.00 MiB |
| `C/configurations/` | 174 | 1.85 MiB |
| `C/deployment-manifests/` | 44 | 0.18 MiB |
| `C/simulations/` | 22 | 2.60 MiB |
| `C/datasets/` | 3 | 26.61 MiB |
| Repo `private/run_store/` | 47 | 8.56 MiB |
| `~/.llm_d_bench/run_store/` | 12 | 6.82 MiB |
| Repo `.dev-logs/` | 9 | 1.10 MiB |
| Repo `.cache/reuse/`, including retrieval models | 18 | 135.59 MiB |

The evaluation directory contains 17,739 log files, 2,057 YAML files, and 78 PNG images. At the time of inventory, the new data directory had only 1 regular file, and the new cache and log directories did not yet exist.

### Original Storage Paths and Their Impact

| Problem | Impact |
| --- | --- |
| Persistent files placed in cache directories | Clearing the cache may delete configurations, results, and connection files |
| Task records and results are scattered | Backup and recovery require locating multiple directories |
| Configurations are saved by display filename | Configurations with the same name may overwrite each other |
| Logs lack completeness markers | It is hard to distinguish complete logs, truncated logs, and diagnostic fragments |
| File references rely on host absolute paths | References may become invalid after changing the host or service user |

## 2. Unified Storage Scheme

### Storage Method

| File category | Storage method | Organization rule |
| --- | --- | --- |
| Files that must be retained, such as deployments, evaluations, and simulations | Regular files in the application's persistent directory | Split by function and task ID, with related file manifests |
| Source code, tokenizers, and tools | Downloaded files in the application's cache directory | Split by resource type and existing repository/version rules |
| Service logs | Text files in the application's log directory | One separate file per startup |
| Temporary rendering and runtime intermediate files | Regular files in the application's temporary directory | Managed and cleaned up by the owning task |
| Model weights | Selected node directory, NFS, or PVC | Mounted in the container at `/model-cache`, preserving the model-cache layout |

### Root Directories

| Type | Default path in development | Container path | Config item |
| --- | --- | --- | --- |
| Persistent data | `~/.local/share/lens/` | `/var/lib/lens/` | `LENS_DATA_DIR` |
| Software and tool cache | `~/.cache/lens/` | `/var/cache/lens/` | `LENS_CACHE_DIR` |
| Service logs | `~/.local/state/lens/logs/` | `/var/log/lens/` | `LENS_LOG_DIR` |
| Temporary files | `~/.cache/lens/tmp/` | `/var/cache/lens/tmp/` | `LENS_SCRATCH_DIR` |
| Runtime files | `$XDG_RUNTIME_DIR/lens/`; if unset, use `runtime/` under the temporary root | Same rule as left | `LENS_RUNTIME_DIR` |
| Model weights | Selected cluster storage volume | Selected cluster storage volume | Storage-volume configuration |

Root directories prefer `LENS_*`; when unset, XDG rules and the defaults above are used. Legacy directory environment variables for each module no longer participate in path selection. Existing source-location configuration such as `LLM_D_ROOT` continues to be used as external-resource input.

### Persistent Data Categories

| Subdirectory | Content | Management requirement |
| --- | --- | --- |
| `artifacts/` | Configuration files, deployment logs, evaluation and simulation results | Split by configuration or execution ID, with file manifests registered |
| `metadata/` | Task states, model registration, and storage-volume registration | Back up together with associated files |
| `datasets/` | Request replay datasets and indexes | Retain source and version information |
| `credentials/` | Cluster connection files, installation credentials, and external service configuration | Restrict access; do not enter public result manifests |

### Directory and File Naming

| Type | Rule | Example |
| --- | --- | --- |
| Task results | Use run or task ID | `evaluations/<runID>/` |
| Configuration version | Independent configuration ID per save, while preserving the original filename | `configurations/<configurationID>/model.yaml` |
| Third-party output | Preserve the tool's original directories and file names | `profile_export.jsonl` |
| New self-owned files | Lowercase English, words joined with hyphens | `evaluation-record.json` |
| Service logs | Service name, UTC time, and process ID | `backend-<UTC time>-<PID>.log` |
| File manifests | Fixed name for task use; independent manifest for datasets in shared directories | `manifest.json`, `<dataset-filename>.manifest.json` |
| Cross-service file reference | Owner type, owner ID, relative path | `lens-artifact://evaluation/<runID>/process.stdout.log` |

### File Manifest Fields

| Registration item | Content |
| --- | --- |
| Owning object | Function type, task ID, or configuration ID |
| Time | Creation time, update time |
| Source version | Software version, source commit, or dataset version; mark as unknown if missing |
| Configuration association | Configuration ID used when producing the result |
| File information | Relative path, type, size, SHA-256 |
| Completeness | Whether truncated, interrupted, or incompletely collected |
| Retention category | `configuration`, `evidence`, `diagnostic`, `cache` |
| Stable identifier | `lens-artifact://` URI, not a host absolute path as the permanent identifier |

## 3. Current Code Paths and Integration Scope

`DATA=~/.local/share/lens`, `CACHE=~/.cache/lens`, `LOG=~/.local/state/lens/logs`. The following are the write locations defined by the code.

### Business Files

| Function and content | New-code default location | Storage method |
| --- | --- | --- |
| Configuration YAML | `DATA/artifacts/configurations/<configurationID>/<original filename>` | Separate directory for each save, with file manifest |
| Configuration snapshot | `DATA/metadata/configurations/<configurationID>.json` | Save the complete configuration and associate the corresponding YAML |
| Deployment manifests | `DATA/artifacts/deployment-manifests/<content digest>/` | Identify same content by digest, with file manifest |
| Deployment tasks and status | `DATA/metadata/deployments/{runs,executions}/` | One JSON file per record |
| Deployment logs and diagnostics | `DATA/artifacts/deployments/<executionID>/` | `logs/entries.jsonl`, diagnostic files, and file manifest; explicitly treated as collected fragments |
| Evaluation task records | `DATA/metadata/evaluations/{runs,workflows}/` | Save state and linkage to result files |
| Evaluation logs, metrics, and charts | `DATA/artifacts/evaluations/<runID>/` | Preserve the tool's original directories; additionally save process logs, result records, and file manifest |
| Simulation tasks and results | `DATA/artifacts/simulations/<taskID>/` | Task records, runner output, and file manifest |
| Trace datasets | `DATA/datasets/` | Preserve original file names and save download info, indexes, and each file's manifest |
| Cluster connection configuration | `DATA/credentials/clusters/{clusters,cluster_sessions}/` | Save cluster registration and kubeconfig |
| Cluster installation files | `DATA/credentials/bootstrap/<installationTaskID>/` | Save files required for installation; temporary keys continue to be cleaned up with the installation task |
| External AI Provider configuration | `DATA/credentials/ai-providers/` | Access-restricted configuration files |
| Model-cache registration | `DATA/metadata/model-cache/entries/` | Register model information; model weights remain on the cluster volume |
| Storage-volume registration | `DATA/metadata/storage/volumes/` | Save storage-volume information |
| Agentic run registration | `DATA/metadata/agentic/benchmarks/` | Save run records |
| Monitoring installation records and logs | `DATA/metadata/monitoring/operations/`; `DATA/artifacts/monitoring/<operationID>/` | Save task state separately; after completion save diagnostic files and manifests |
| Temporary deployment files | `CACHE/tmp/` | Separate from the source tree |
| Service logs | `LOG/dev/`, `LOG/service/` | A new log file for each startup, while preserving the latest-log entry point |

### External Source Code, Tools, and Model Cache

| Resource | Path | Usage |
| --- | --- | --- |
| llm-d source used for development startup | `CACHE/llm-d/` | Default location for `scripts/dev.sh`; `LLM_D_ROOT` can specify an existing directory, and if absent a prompt is shown to prepare the source |
| Repository download feature | `CACHE/repos/<repo>/<processed-version-name>/` | Cache llm-d and llm-d-benchmark by repository and version |
| Guide planning cache | `CACHE/guide-planning/` | Save manifests and rendered results used for planning |
| Remote deployment source | Remote host `CACHE/deploy-repos/<org>/<repo>/` | A GitHub URL triggers cloning; if an existing path is specified, use that path |
| Kubespray | `CACHE/kubespray/` | Cluster-installation tool cache |
| Simulation backends | `CACHE/backends/` | Downloaded runner tools |
| Simulation tokenizer | `CACHE/tokenizers/<model-name>-<hash>/` | Model name normalized and suffixed with a digest |
| Model weights | `hub/models--<org>--<model-name>/` in the selected storage volume | The download container mounts it as `/model-cache`, using the Hugging Face cache layout |

Source downloads preserve the existing repository, version, and deployment-scenario distinction, and do not add a new commit-based deduplication mechanism. The actual node or NFS path for model weights is determined by the storage volume; for example, if the volume root is `/data/models`, the model is at `/data/models/hub/models--<org>--<model-name>/`. `DATA/metadata/model-cache/` stores only registration information.

### Integrated Shared Capabilities

| Function | Existing capability | Code entry |
| --- | --- | --- |
| Unified root directories | Python, Node, and startup scripts use `LENS_*` / XDG rules; old module directory variables no longer override new paths | `utils/paths.py`, `server/storagePaths.ts`, `scripts/storage-env.sh` |
| Business-file persistence | Configuration, deployment, evaluation, simulation, and monitoring are saved by domain and task ID | Modules in each domain |
| File registration | Record source version, configuration ID, SHA-256, truncation status, and retention category | `utils/artifact_store.py` |
| File location | Form a `lens-artifact://` identifier from owner object ID and relative path | `artifact_uri`, `resolve_artifact` |
| External resource locations | Source code and tools use a unified cache root; model weights use the selected storage volume | Repository download, Guide, Model Cache |
| Offline management tools | Historical file copying, manifest reconstruction, expiration-candidate statistics; preview by default, copy requires explicit execution | `utils/storage_admin.py` |

All `utils/` entry points in Python are under `llm_d_bench/`.

### Shared Entry Points and Integration Code

| Capability | File path | Integration scope |
| --- | --- | --- |
| Python path selection | [llm_d_bench/utils/paths.py](../../llm_d_bench/utils/paths.py) | Persistent directories, cache, and temporary directories for Python business modules |
| Node path selection | [server/storagePaths.ts](../../server/storagePaths.ts) | Cluster sessions, Guide planning cache |
| Startup environment | [scripts/storage-env.sh](../../scripts/storage-env.sh) | Root-directory configuration for development and installation startup scripts |
| File manifests and identifiers | [llm_d_bench/utils/artifact_store.py](../../llm_d_bench/utils/artifact_store.py) | Configuration, deployment, evaluation, simulation, dataset, and monitoring results |
| Migration and retention checks | [llm_d_bench/utils/storage_admin.py](../../llm_d_bench/utils/storage_admin.py) | Offline copying, manifest reconstruction, cleanup-candidate statistics |
| Container storage | [Dockerfile](../../Dockerfile) | Service root directories and volume configuration |

### Content Kept Under Separate Management

| Content | Location |
| --- | --- |
| Raw KV records inside Pods | Inference environment `/tmp/prism-kv-trace/`, supports `PRISM_KV_TRACE_DIR`; aggregated results are saved with evaluation results |
| Assistant conversations, recordings | Browser memory; not integrated into a unified server-side archive |
| Exported reports | Browser download directory |
| Development retrieval data | Repo `.cache/reuse/` |
| Service process ID files | Repo `.dev-pids/`, `.prism-pids/` |

### Existing Feature Boundaries

| Item | Current behavior |
| --- | --- |
| Trace datasets | Original filenames, indexes, and independent manifests are saved under `DATA/datasets/`; no new multi-version directory is added |
| Tool output | Preserve the original directories and file names of third-party tools |
| Source cache | Unified cache root directory, preserving existing download and version-selection behavior |
| Retention category | Used for registration and candidate statistics; does not trigger scheduled deletion |
| Conversations and recordings | Preserve the browser's original handling; do not add server-side archival or upload |
| Storage media | Local file systems and cluster storage volumes; no object storage introduced |
| Validation scope | Local paths, file manifests, and caller regression tests; real-cluster validation is not a prerequisite for path design |
| Runtime and historical data | Services were not restarted and historical files were not migrated in this round; new rules take effect when new code is loaded |

## 4. Unified Read/Write and Integration Rules

The following are development constraints for this scheme, applicable to maintenance of existing features and development of new features. New files produced by existing features are stored according to the paths in Section 3; new file types select the same set of root directories by purpose and are integrated by the corresponding modules through the shared path and file-registration entry points.

| Integration scenario | Directory selection | Integration method |
| --- | --- | --- |
| Existing evaluations add logs or images | `DATA/artifacts/evaluations/<runID>/` | Reuse that evaluation's result directory and include them in its file manifest |
| New task files that require persistent retention | `DATA/artifacts/<function>/<taskID>/` | Use `storage_path` to determine the directory and `register_artifacts` for registration |
| New tool resources that can be redownloaded | `CACHE/<resource type>/` | Python uses `storage_path`, Node uses `storagePath` |
| New model weights | Model-cache directory in the selected storage volume | Reuse the storage-volume and Model Cache flow |

### Read/Write Constraints

| Management item | Requirement |
| --- | --- |
| Storage entry | Reuse shared path functions; do not add new module-level storage-root variables |
| Read/write consistency | Use the same directory rules for write, read, download, and delete; when adjusting paths, check all callers at the same time |
| Persistent results | Configuration, reproduction inputs, key logs, and formal results go into persistent-data directories |
| Cache | Store only content that can be redownloaded or regenerated; it must not be the only copy of results |
| Execution isolation | Each execution uses an independent ID; do not overwrite historical results by display name |
| Source traceability | Register source version, configuration ID, file checksum, and completeness |
| Large files | Save separately from task records and associate them through stable identifiers |
| Retention category | Register file purpose; automatic cleanup must be defined separately and is not performed by path functions |
| File location | Locate through owner object ID, relative path, and file manifest; do not use host absolute paths as permanent identifiers |
| Migration acceptance | Check file count and checksums, validate task references, and record missing items and conflicts |
