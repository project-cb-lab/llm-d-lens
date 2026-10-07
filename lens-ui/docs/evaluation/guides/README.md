# Detailed Introduction to Evaluation Guides

[Overall Workflow and Features](../README.md) · [Parameter Reference](../parameters/README.md)

Guide defines the deployment and optimization scheme, and variant specifies the concrete implementation path. Deployable combinations are jointly determined by the provider, source version, and cluster capabilities. The deployment and workload configuration below are referred to as inputs, and the evaluation metrics and mechanism evidence are referred to as outputs; output availability depends on actual collection results.

## 1. Solution Overview

| Guide | Identifier | Core mechanism | Evaluation focus |
| --- | --- | --- | --- |
| Optimized Baseline | `optimized-baseline` | Approximate prefix affinity and token-load routing | Combined effect of cache reuse and load balancing |
| PD Disaggregation | `pd-disaggregation` | Separate Prefill/Decode pools and KV transfer | Generation stability, resource ratios, and transfer cost |
| Precise Prefix Cache Routing | `precise-prefix-cache-routing` | Instance-level block index driven by KV events | Whether cache-location awareness improves actual reuse |
| Tiered Prefix Cache | `tiered-prefix-cache` | Extend KV from HBM to lower-level cache | Benefit of restore vs. recomputation under capacity pressure |

All Guides share model, image, storage, workload, SLO, and execution-control parameters, and output throughput, latency, success rate, and available resource evidence. See the [parameter reference](../parameters/README.md) for the complete field scope.

## 2. Optimized Baseline

Select affinity instances based on approximate prefix history, and combine instance token load and saturation protection to route requests. The goal is to improve prefix reuse while controlling queueing and hot-spot skew.

### Inputs

| Parameter | Purpose |
| --- | --- |
| `decode.replicaCount`、`decode.tensorParallelSize` | The number of aggregated instances and TP per instance, determining capacity and routing scope |
| `guideSettings.routerValues` | Override routing configuration for prefix affinity, token-load scoring, and calibration |
| `maxModelLen`、`maxNumSeqs`、`gpuMemoryUtilization` | Control context, engine concurrency, and VRAM budget |
| `shared_prefix.*` | Set prefix-group count, reuse length, and request shape |
| Concurrency or rate ladder | Observe behavior under low load, stable sharing, and hot high load |

### Outputs and Focus

| Output | Explanation and use |
| --- | --- |
| TTFT, output throughput, success rate | Determine the impact of routing optimization on user experience and service capacity |
| Engine prefix counter ratio | Engine cache-counter ratio, used to observe reuse changes; it is not a request hit rate |
| Endpoint token load, CV, queue | Observe instance load distribution and hot spots; CV is based on per-endpoint window means |
| SLO and stable capacity | Determine whether reuse gains also satisfy latency and success-rate requirements |

The main comparison is Full vs. `load-only` to evaluate the contribution of prefix affinity; add `affinity-only` to observe the contribution of load scoring. Affinity-only still includes saturation protection. Experiments should cover both non-reuse and shared-prefix traffic while controlling model, topology, and engine parameters.

The focus is whether reuse, load, and tail latency improve together. Without request-to-Pod correlation evidence, prefix-routing correctness cannot be verified per group; load comparison across heterogeneous instances also requires capacity normalization.

## 3. PD Disaggregation

The Prefill pool computes inputs, the Decode pool generates outputs, and KV is transferred through the connector. The two sides configure resources independently to reduce cross-stage interference and adapt to different input/output ratios.

### Inputs

| Parameter | Purpose |
| --- | --- |
| `prefill.replicaCount`、`prefill.tensorParallelSize` | Replicas and TP for the Prefill pool |
| `decode.replicaCount`、`decode.tensorParallelSize` | Replicas and TP for the Decode pool |
| `pdTopologyVariants` | Scan P:D topology combinations |
| `guideVariant`、connector and network configuration | Determine the KV handoff implementation and communication conditions |
| `guideSettings.rdmaNicCount` | NIC count per Pod used only by `vllm-rdma`, independent of TP |
| `matrix[].isl/osl`、load ladder | Distinguish long input, long output, and mixed pressure |

### Outputs and Focus

| Output | Explanation and use |
| --- | --- |
| TTFT | Includes the impact of input computation, scheduling, transfer, etc.; should be judged together with generation latency |
| ITL, TPOT, and their tail latencies | Measure generation-stage stability; the two metrics are not interchangeable |
| P/D queue, running requests, device utilization | Locate resource-ratio imbalance between the two sides |
| KV transfer bandwidth, latency, and failure rate | Measure the communication cost of stage separation; transfer latency may be a rolling-P95 window mean |
| `decode_interference_penalty` | The increase of max ITL P95 in later rate stages relative to the first stage, describing only cross-stage change |

The main baseline is an aggregated deployment with the same total GPU count, while scanning P:D ratios. Total GPU count is the sum across both sides, for example P=1×TP2 and D=2×TP1 equals 4 GPUs.

The focus is whether improved generation stability offsets TTFT and network cost. Verifying interference from Prefill bursts requires additional orchestration of burst traffic and aligned request timelines; latency differences across stages alone cannot prove causality.

## 4. Precise Prefix Cache Routing

Model instances publish KV events, and the routing side maintains a block index organized by instance. After render/tokenizer, requests query cache location and then combine it with load to select an instance, reducing incorrect affinity and duplicate computation.

### Inputs

| Parameter | Purpose |
| --- | --- |
| Model, tokenizer, render configuration | Ensure that request token identity and index semantics are consistent |
| `blockSize` | Ensure the engine and precise index use the same KV block size |
| KV event publish, subscribe, and index configuration | Provide cache-location updates, usually defined by Guide templates and routing configuration |
| `guideSettings.routerValues` | Override index and routing policies; current token-load accounting requires one EPP replica |
| Replicas, TP, shared prefixes, and rate ladder | Control capacity, reuse, and eviction pressure |

### Outputs and Focus

| Output | Explanation and use |
| --- | --- |
| Actual engine prompt reuse | Infer actual reuse based on same-window token-source ratios among local cache, external KV, and local computation |
| Local compute, cached-token recompute | Distinguish local computation from cached-token recomputation; computation of new input cannot all be called recomputation |
| TTFT, throughput, success rate | Determine the actual performance effect of precise positioning |
| Lookup, admission, eviction, subscriber | Check the index query, update, and subscription chain |
| Router cached-token estimate | A routing-side reuse estimate, which must be kept separate from actual engine reuse |

The main comparison is Approx (Optimized Baseline) vs. Precise, keeping model, tokenizer, block size, GPU count, and traffic the same. Shared prefixes can cover basic reuse and capacity pressure; restart, scale-out/in, and index recovery require additional orchestration.

The focus is changes in actual engine reuse and TTFT. Index activity can only prove that the chain is active; by itself, it cannot prove reduced prefill computation. Same-Pod Service baselines can compare entry paths, but they inherit cache state.

## 5. Tiered Prefix Cache

Write KV out from HBM to a lower-level cache and restore it on revisit, reducing recomputation caused by insufficient capacity. The current Configuration supports Native CPU and LMCache CPU paths, but not filesystem offload.

### Inputs

| Parameter | Purpose |
| --- | --- |
| `guideVariant` | Select a supported path such as `native/cpu/base` or `lmcache-connector/cpu/base` |
| `guideSettings.cacheCpuGiB` | CPU cache capacity per Pod; backend mapping differs between Native and LMCache |
| `gpuMemoryUtilization`、engine KV configuration | Affect the HBM cache budget; actual capacity must be read from deployment and monitoring |
| `shared_prefix.num_groups/system_prompt_len` | Control configuration prefix scale |
| Revisit order, rate ladder | Cover first use, capacity pressure, and revisit; exact order can be defined with custom workloads |

### Outputs and Focus

| Output | Explanation and use |
| --- | --- |
| Restore/offload rate, restore time | Determine actual write-out, restore, and their cost; bytes estimated by rate mean × window duration are only approximations |
| TTFT, throughput, success rate | Determine the performance gain of restore relative to recomputation |
| HBM peak and capacity, CPU usage and capacity | Analyze capacity pressure and host-memory cost |
| External cache counter ratio | Signal of external-cache reuse; it cannot be split into CPU vs. FS hits without evidence |
| `metrics.distinct_kv_working_set` | Deduplicated logical KV working set of complete prompt blocks from successful requests, requiring complete collection evidence |
| Configured prefix scale | `group count × shared prefix length`, a configuration estimate rather than a measured KV working set |

The main baseline is HBM-only vs. Tiered under the same backend, routing, and resource conditions, observing revisits after HBM capacity is exceeded. High utilization does not prove effective restore occurred; the sum of HBM and CPU capacity is also not the deduplicated effective cache capacity.

Automatic KV collection requires compatible vLLM V1, prefix caching, a single FullAttention cache group, and `parallelism=1`. Compatible newly created deployments will mount a probe, while existing deployments are not injected online, and PD is not injected for now. The measured scope excludes generated tokens, incomplete trailing prompt blocks, and failed requests; whole-run multi-stage evidence is not automatically apportioned to each stage. When records are missing or request association is incomplete, the missing reason is displayed.

## 6. Baselines and Comparison Conditions

| API identifier | Meaning |
| --- | --- |
| `direct-vllm` | A separately deployed pure vLLM, used to compare the full serving stack; topology and GPU-count differences must be checked |
| `router-neutral` | A neutral-routing reference, currently using the random picker |
| `router-round-robin` | A compatibility alias of `router-neutral`; it does not imply strict round-robin |
| `load-only` | An ablation configuration that retains load routing |
| `affinity-only` | An affinity policy that removes load scoring while retaining saturation protection |
| `optimized-baseline` | The Optimized Baseline Guide, which can serve as the Approx baseline for Precise |
| `kubernetes-service` | Reuses the candidate's Pods and bypasses EPP; it does not guarantee strict per-request round-robin and inherits the candidate's cache |

Available options depend on provider capabilities. Comparisons should standardize model and software version, resource budget, input/output shape, load, cache state, and SLO, and present absolute values and rates of change at the same measurement point. Mechanism conclusions must use measured evidence from the corresponding window.
