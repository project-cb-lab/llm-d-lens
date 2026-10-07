# Evaluation Parameter Reference

[Overall Workflow and Features](../README.md) · [Detailed Guide Introduction](../guides/README.md)

> Parameters are categorized into deployment configuration, workload, SLO, and execution control. API and UI support scope are marked separately.

## 1. Environment and Deployment Parameters

| Group | UI / saved field | Description |
| --- | --- | --- |
| Environment | Cluster / `cluster_session_id` | Kubernetes cluster and established connection session |
| Model | `model.name` | Model identifier; results attach the actual revision, tokenizer, and chat template (when recorded) |
| Engine and image | `runtime.modelServer`、`runtime.image`、`imageMode` | Determine the software stack; record the actual image/digest |
| Model storage | `modelSource`、`storageVolumeId`、`mountPath` | Cache storage or shared model path; new and reused configurations must match Setup |
| Credentials | `modelSecret.mode/sourceNamespace/sourceName` | `none / host / existing-secret`; an existing Secret requires namespace and name |
| Scheme | `provider_ref`、`guideVariant` | Guide identifier and concrete variant |
| Regular deployment | `decode.replicaCount`、`decode.tensorParallelSize` | Replica count and TP per replica; aggregated deployments may also be saved using decode fields |
| PD deployment | `prefill.*` + `decode.*` | Configure replicas and TP independently for both sides; record replica ratio and GPU ratio |
| Topology scan | Replica list, TP list, `pdTopologyVariants` | Such as `1,2`、`1:1,1:3`; the current frontend expands at most **16** topology combinations |
| Source snapshot | `officialGuide.source` | Source information such as repository, requestedRef, commit, files, and variant |

| vLLM configuration field | Corresponding parameter | Current frontend constraint / behavior | Effect |
| --- | --- | --- | --- |
| `maxModelLen` | `--max-model-len` | Positive integer; inherit the Guide when empty | Context limit |
| `maxNumSeqs` | `--max-num-seqs` | Positive integer; inherit when empty | Max concurrent engine sequences |
| `gpuMemoryUtilization` | `--gpu-memory-utilization` | `0 < x ≤ 1`; inherit when empty | VRAM budget |
| `blockSize` | `--block-size` | Positive integer; still must pass Guide/engine compatibility validation | KV block size and precise-index consistency |
| `maxNumBatchedTokens` | `--max-num-batched-tokens` | Positive integer; inherit when empty | Batch token budget |
| `customParameters[]` | Argument or environment variable | `target=prefill/decode/both`; `kind=argument/environment` | Override by role; validate same-name conflicts |

> Model and TP use dedicated controls; custom parameter names do not include `--`. Valid values are constrained by the engine and templates. For single-node pure TP, estimated GPU count is `replicas × TP`; actual GPU count is determined by resource requests, and for PD it is the total across both sides.

## 2. Guide-specific Parameters

| Field | Available scope | Validation method | Display focus |
| --- | --- | --- | --- |
| `guideSettings.cacheCpuGiB` | Tiered: `native/cpu/base`, `lmcache-connector/cpu/base` | Positive number per Pod; Native maps to `cpu_bytes_to_use=GiB×1024³`, LMCache maps to `LMCACHE_MAX_LOCAL_CPU_SIZE` | CPU cache capacity |
| `guideSettings.rdmaNicCount` | PD: only `vllm-rdma` | Positive integer; other variants do not accept it | Network resource configuration |
| `guideSettings.routerValues` | Routing YAML override entry | YAML mapping text; merge base, Guide, and user override layers, then validate the final configuration | Routing strategy and configuration differences |
| Routing affinity, load scoring, calibration | Via Guide router values / templates | Configured through templates or router values | Baseline / Precise focus |
| KV events, block size, render service | Precise templates and related configuration | Publisher, router index, and model must align | Precise focus |
| Connector, PVC, storage path | Corresponding PD / Tiered templates | Availability is determined by variant, hardware, and deployment validation; current Configuration does not support filesystem offload | PD / Tiered focus |

## 3. Workload Configuration

| Mode | Field | Load generation method | Evaluation use |
| --- | --- | --- | --- |
| Fixed input / output lengths | `matrix` + `concurrency_stages` | Closed-loop fixed concurrency | Impact of input/output length and concurrency on performance |
| Shared prefixes | `shared_prefix` | Open-loop Poisson arrival-rate stages | Prefix reuse, cache capacity, and pressure upper bound |
| Repository profile | `workload` | Repository YAML definition | Reuse repository workload |
| Custom YAML | `workload_yaml` | Custom YAML definition | Custom data, RPS experiments without shared prefixes, etc. |

**The three generation methods `matrix`, `shared_prefix`, and `workload_yaml` are mutually exclusive; if none is enabled, the repository workload is used.**

### Fixed Lengths and Concurrency Sweep

| Field | API range / default | Unit / meaning |
| --- | --- | --- |
| `matrix[].isl`、`.osl` | Each `1–1,000,000`; up to 50 length points | Input / output tokens; run point by point |
| `concurrency_stages[].concurrency` | `1–4,096`; up to 20 stages | Number of simultaneously in-flight requests; not RPS |
| `concurrency_stages[].num_requests` | `1–100,000` | Number of formal requests per length point per stage |
| `warmup_requests` | Default **2**; `0–50` | One unscored warm-up before each target's sweep, using the first-point ISL/OSL and concurrency 1; 0 disables it; Matrix only |
| Generation behavior | streaming, `ignore_eos=true`, fixed-length distribution | Synthetic workload; actual length is determined by results |

When Matrix omits `concurrency_stages` in the API (or passes an empty list), it uses `concurrency/request count = 1/32, 8/96, 32/96, 64/192`; the UI requires at least one stage, and the UI presets differ from this fallback. Total formal request count is `target count × length point count × parallelism × Σ num_requests`, excluding warm-up.

### Shared Prefixes and Arrival-rate Sweep

| Field (all under `shared_prefix`) | API range / default | Meaning |
| --- | --- | --- |
| `num_groups` | `1–100,000` | Number of distinct shared-prefix groups |
| `num_prompts_per_group` | `1–10,000` | Number of samples per group; not the total number of actual sent requests |
| `system_prompt_len` | `1–1,000,000` tokens | Shared prefix length per group |
| `question_len` | `1–1,000,000` tokens | Unique input length per request |
| `output_len` | `1–1,000,000` tokens | Target output length |
| `stages[].rate` | `0 < rate ≤ 10,000` req/s | Target arrival rate; actual successful throughput is not guaranteed to reach it |
| `stages[].duration` | `1–86,400` seconds; up to 50 stages | Load duration per stage; run in list order |
| Inter-stage gap (generation behavior) | Fixed `load.interval=0` | No extra pause between stages; the `shared_prefix` API has no `interval` field, and the UI has no separate control |
| `enable_multi_turn_chat` | Default **false** | Multi-turn prefix reuse; once history grows, context must be checked additionally |

| Validation / estimation | Definition |
| --- | --- |
| Matrix context | `ISL + OSL ≤ the smallest effective context limit among selected configurations` |
| Shared-prefix first-round context | `shared length + unique input + output length ≤ context limit`; templates and multi-turn still require runtime validation |
| Shared-prefix scale | `group count × shared length`, in tokens; a workload-derived quantity, **not a measured KV-byte working set** |
| Total configured load duration | `target count × Σ duration`; excludes deployment, preparation, and request draining |
| Expected arrival request count | Approximately `parallelism × Σ(rate × duration)` / target; actual counts are based on raw results |

The shared-prefix API accepts any valid rate greater than 0; the current frontend validation lower bound is `0.001` req/s. The generation mode does not clear cache between stages, and same-Pod comparisons also retain cache; stage order and preceding traffic affect results. Automatic KV collection currently requires `parallelism=1`; multiple workers will record an unsupported reason.

### Current UI Presets

| Preset | ISL / OSL | Concurrency | Requests per stage |
| --- | --- | --- | --- |
| Quick check | `128 / 64` | `1` | `10` |
| Concurrency sweep | `1024 / 128` | `1 / 8 / 32 / 64` | `100 / 100 / 128 / 256` |
| Long inputs | `4096,8192,16384 / 256` | `1` | `100` |
| Guide defaults | Provided by the provider | Provided by the provider | Varies by Guide |

## 4. SLO and Execution Control

| Field | Default / boundary | Purpose and UI scope |
| --- | --- | --- |
| `sla_targets.ttft_ms`、`tpot_ms` | Unset by default; `0 < x ≤ 3,600,000` ms | Upper limit for time to first token / time per output token |
| `ttft_percentile`、`tpot_percentile` | API default **p99**; p50/p90/p95/p99 | Percentile must be shown together with the threshold |
| `success_rate_min_percent` | Unset by default in API; default **99** in UI; `0–100` | Minimum success rate; capacity derivation uses 99% when unset |
| `throughput_min_tps` | Unset by default in API; `≥ 0` | Minimum output throughput; current BenchmarkInputs has no dedicated control |
| `harness`、`workload` | API default `inference-perf`, `sanity_random.yaml` | The current wizard runner is inference-perf; called by llmdbenchmark |
| `parallelism` | Default **1**; `1–32` | Number of independent load-generator workers; each worker executes the same workload, multiplying total traffic |
| `harness_memory_gib` | Default **32**; integer `1–512` GiB | Benchmark → Advanced: host-memory request and limit for each benchmark worker, also overriding the hardware overlay; not model VRAM. Total reserved memory increases with `parallelism`, and the node must have enough schedulable memory |
| `wait_timeout_seconds` | Default **7,200**; `1–14,400` seconds | Passed to each llmdbenchmark invocation as a wait timeout; not the total timeout of the whole Evaluation (including deployment) |
| `accelerator_profile`、`storage_class_name` | Optional in API | Benchmark hardware profile / storage; current main form has no dedicated controls |
| `runtime.http_proxy/https_proxy/no_proxy` | Empty strings by default in API | Execution network environment |
| `benchmark_source.repository/revision` | Optional in API | Specify the benchmark source repository and revision |
| `include_baseline`、`baseline_types` | API defaults to direct-vllm enabled; list up to 7 items | UI generates baseline groups according to user selection |
| `baseline_parameters` | Optional in API | Override replicas, TP, context, sequence count, VRAM ratio, block size, and batch tokens by baseline |
| `compare_configurations` | API default **true** | The wizard enables pairwise comparison only when there are multiple candidates |
| `preserve_deployment` | Default **false** | Retain successful candidate deployments created by this task |
| `benchmark_plans`、`scenarios` | 1–50 Plans per Evaluation; up to 20 Scenarios per Plan | The current wizard submits one unified scenario; the API can reuse multi-scenario suites |

### API-level and Baseline Constraints

- `parallelism`, `matrix`, `shared_prefix`, etc. belong to `benchmark`. When using multiple scenarios, place them under `benchmark_plans[].scenarios[].benchmark`; place SLOs under the same scenario's `sla_targets`.
- When `scenarios` is empty, the Plan's `benchmark` is used; when non-empty, each Scenario uses its own complete `benchmark` and does not merge or inherit fields from the Plan.
- `include_configuration` defaults to `true`; when disabled, at least one baseline must remain. `baseline_types` accepts `direct-vllm`, `router-neutral`, `router-round-robin`, `load-only`, `affinity-only`, `optimized-baseline`, and `kubernetes-service`; `router-round-robin` is normalized to `router-neutral` and deduplicated, while actual availability still depends on Guide capability restrictions.
- `kubernetes-service` reuses the Full Guide's model Pods and requires `include_configuration` to be enabled at the same time. `preserve_deployment` only retains successful candidate deployments; independent baselines are still cleaned up.
- `baseline_parameters` uses snake_case: `replicas=1–32`, `tensor_parallel_size=1–16`, `max_model_len=1–1,000,000`, `max_num_seqs=1–100,000`, `gpu_memory_utilization=0.1–1`, `block_size=1–1,024`, `max_num_batched_tokens=1–1,000,000`; these are the ranges of the baseline-override API and are not the same as the configuration-editor constraints above.
- `workload_yaml` supports up to 262,144 characters and must contain `load`, `api`, and `data` mappings; regular workload types must have non-empty `load.stages` or `load.sweep`. `workload` is a file name inside the repository and cannot be a directory path.

## 5. Task and Plan Fields

The following are Evaluation API fields; deployment configuration uses the saved fields above, and the two have different namespaces.

| Level / field | Default or constraint | Description |
| --- | --- | --- |
| Evaluation `name` | Default `Evaluation`; 1–200 characters | Task name |
| Evaluation `cluster_session_id` | Required; 36 characters | Current cluster connection session |
| Plan `id` | 1–100 characters; starts with a letter or number, followed by `_` or `-` | Unique within the same task |
| Plan `configuration_artifact_id` | Required; 36 characters | Saved deployment configuration ID |
| Plan `deployment_name/description` | Optional; name 1–120 characters, description up to 1,000 characters | Identifier and description for a self-managed deployment |
| Plan `include_configuration` | Default true | Whether to execute the candidate configuration |
| Plan `baseline_type` | Default `direct-vllm` | Single-baseline fallback when `baseline_types` is empty |
| Scenario `id/name/description` | ID follows the same format as Plan; name 1–200 characters; description up to 1,000 characters | ID unique within the Plan; description defaults to an empty string |
| Scenario `benchmark/sla_targets` | Defaults to the respective model defaults | Workload and SLO for the scenario; does not inherit Plan fields |
| `benchmark_source.repository/revision` | HTTPS repository URL, up to 500 characters; revision 1–128 characters | Fixed benchmark source |
| `runtime.http_proxy/https_proxy/no_proxy` | Length limits are 1,000/1,000/2,000 characters respectively | Proxy settings |

## 6. Output Metrics

| Metric / field | Unit and meaning |
| --- | --- |
| TTFT | ms; time from request start to first token |
| ITL | ms; interval between adjacent output tokens; streaming chunk sampling must state the granularity |
| TPOT | ms/token; `(completion−first token)/(output tokens−1)`, undefined when there are fewer than 2 output tokens |
| E2E / Request latency | ms; time from request start to completion |
| Output throughput | tok/s; actual output token count divided by measurement duration |
| Request throughput | req/s; actual request throughput, distinct from target rate |
| Success / Error rate | %; calculated from raw counts of success, failure, and total requests |
| `metrics.latency_distributions` | Latency distribution; mean, P50, P95, and P99 recorded separately |
| `matrix_results[].stage_metrics` / `rate_stage_results` | Per-stage results for fixed-length / arrival-rate modes |
| `observability` | Monitoring summary, time series, roles, endpoints, and collection window |
| `resource_snapshot` | Saved resource conditions, not a substitute for per-stage monitoring |
| `sla.met` | true / false / null; met, not met, or cannot be determined |
| `maximum_stable_qps` | req/s; the highest tested stable target rate |
| `slo_goodput_rps` | req/s; the maximum actual request throughput among stable stages |
| `metrics.request_slo_goodput` | req/s; rate of successful requests that jointly satisfy per-request latency thresholds, with count, actual duration, thresholds, and source attached |
| `slo_violation_point` | The first unverified stable stage when sorted by rate; may also be caused by missing evidence |
| `saturation_point` | The first tested stage where actual throughput drops below 95% of the target rate |
| `metrics.distinct_kv_working_set` | Deduplicated logical KV tokens plus collection status, scope, and source; see the Guide introduction for specific limits |

ITL percentiles and TPOT percentiles are different; the mean of per-Pod percentiles is not the global percentile. Missing values and zero denominators do not participate in rate-of-change calculations. The general relative change is `(candidate−baseline)/baseline×100%`; latency reduction is `(baseline−candidate)/baseline×100%`.

Per-request goodput uses the configured TTFT/TPOT thresholds; percentile selection does not change per-request conditions. Matrix, Shared-prefix, and Custom YAML automatically enable required request reports; Repository profile must configure them in the file. When stage association is missing, ownership is not inferred by duration.

## 7. Historical Storage Configuration

| Environment variable | Description |
| --- | --- |
| `LENS_DATA_DIR` | Root directory for persistent data; task records are under `metadata/evaluations/`, and local results and reports are under `artifacts/evaluations/`. Old evaluation-directory variables no longer take effect. |

Both directories should be persistent in order to retain historical records after deployment cleanup or application restart.

## Implementation Basis

[API Parameter Models](../../../llm_d_bench/evaluate/models.py) · [Frontend Benchmark Validation / Presets](../../../src/features/evaluation/benchmarkSettings.js) · [BenchmarkInputs](../../../src/components/evaluation/BenchmarkInputs.jsx) · [Topology and Parameter Mapping](../../../src/features/evaluation/configuration.js) · [Configuration Validation](../../../src/features/evaluation/configurationValidation.js) · [Guide Parameter Validation](../../../llm_d_bench/configuration/guide_settings.py) · [Setup Matching](../../../src/features/evaluation/setup.js) · [Configuration Models](../../../llm_d_bench/configuration/models.py) · [Execution and Workload Generation](../../../llm_d_bench/evaluate/router.py)

### Troubleshooting Benchmark Client OOM

`inference-perf-…/harness (terminated: OOMKilled, exit_code=137)` means the benchmark client hit the host-memory limit,
not that the model service lacks GPU memory. The Intel XPU benchmark overlay previously limited the client to 8 GiB; Lens now explicitly overrides
memory request and limit, with a default of 32 GiB per worker, while older templates used `memory` to set both at once.
Existing running Pods do not change when the configuration is modified; a new task is required.

32 GiB is not a capacity guarantee for arbitrary workloads. Long inputs, large datasets, and many in-flight requests can still exhaust memory; increase
`harness_memory_gib` based on observation, or reduce rate / concurrency / dataset size. The duration of open-loop rate stages is the load duration,
not including request draining time; an arrival rate above the model's processing capacity will continue to accumulate backlog.
Results from OOM failures cannot be treated as complete successful measurements.
