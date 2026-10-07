# Agentic Deploy Evidence

This document describes how Agentic Deploy handles structured and unstructured evidence, the current implementation, and next steps.

## Data and authority

### Structured data

Structured data consists of `BenchmarkRecord`: model, hardware, runtime, workload, candidate configuration, measured performance metrics, outcome, and failure reason. Records reside in `agentic_benchmark_records` and are authoritative for performance numbers, failure facts, and candidate-level predictions.

Live cluster overview is also structured evidence, with the highest authority: it determines whether candidates satisfy current VRAM, GPU, CPU-buffer, and other hard constraints. Historical benchmarks, AIC, and AI cannot override live-resource conclusions.

### Unstructured data

Unstructured data includes deployment logs, profiling traces, artifact summaries, Guides, GitHub content, papers, planning-snapshot summaries, and planned raw benchmark reports from cloud storage. Searchable projections reside in OpenSearch; original artifacts remain in controlled filesystems, and cloud reports remain in their authorized storage locations.

Cloud benchmark report integration is not yet implemented. The plan is to index ACL-controlled summaries and object versions for discovery, then fetch the pinned source version under tenant/ACL controls and validate its content hash before use. Extract model, hardware, runtime, workload, topology, measured metrics, outcome, and provenance; validate units and required fields; normalize into request-local `BenchmarkRecord` objects. Reports with missing fields, unverifiable sources, or only estimates cannot become measured records. Unextracted or unvalidated report text is diagnostic only and cannot affect numeric prediction, failure blocking, or deterministic ranking. Qualified records still undergo structured benchmark compatibility filtering and similarity computation; retrieval alone does not establish performance.

Log, trace, artifact, and snapshot summaries serve diagnosis and explanation only. ACL-filtered, version-pinned Guide, GitHub, and paper summaries may also provide AI recommendation features for method applicability, compatibility constraints, and preference tradeoffs. Unstructured data is not authoritative for TTFT, TPOT, throughput, confidence, or SLOs; it cannot directly generate or block candidates or change deterministic ranking.

## Constructing evidence

### Structured benchmark evidence

The server first applies coarse database filters for accelerator, backend, quantization, topology, per-GPU VRAM, and a safe time window. Retain both successful and failed records. `BenchmarkEvidenceRetriever` then computes model, hardware, use-case-derived workload, candidate-configuration, and SLO-performance similarity, plus time/runtime decay, producing two separate views:

| View | Contents | Consumer |
| --- | --- | --- |
| Raw samples | At most five relevant `BenchmarkRecord` objects per candidate, including measured metrics, outcome, failure reason, and `match_similarity` | AI scoring |
| Aggregate inputs | `PerformanceScoreInput` with predicted metrics, status, confidence, effective sample size, failure blocking, and refs | Deterministic Planner |

`match_similarity` expresses only the relevance of a historical record to the current target. It is not deployment success probability or a promise of current performance. Aggregate `confidence` and effective sample size belong only to the deterministic path.

The current request does not require direct workload input. Versioned `resolve_workload_signals()` maps use case to query profile $q$, including token lengths, concurrency, request rate, shared-prefix and prefill signals. Explicit workload overrides only corresponding fields and records provenance. If the mapping is unknown or cannot provide comparison fields, do not fabricate workload; related history cannot become high-similarity performance evidence.

For historical record $r$ passing database hard filters and current candidate query $q$, the retriever computes configuration/workload and performance-target similarity $S$:

$$
S = 0.20S_{model} + 0.15S_{hardware} + 0.15S_{workload} + 0.15S_{configuration} + 0.35S_{SLO}
$$

These weights reflect the decision value of measured historical performance rather than a fit to limited historical samples. $S_{SLO}$ has the largest individual weight, $0.35$, favoring records whose TTFT, TPOT, and workload capacity fit current targets; the weakest-component bound below prevents identical configurations with unsuitable performance from scoring highly. Model, hardware, and workload retain weights $0.20$, $0.15$, and $0.15$ so performance is interpreted in comparable environments and request shapes. Configuration has weight $0.15$ so TP, replicas, and optimizations influence ranking. Hard filtering already ensures accelerator, backend, quantization, topology, per-GPU VRAM, and time-window compatibility; these are not counted again.

These coefficients, subcomponent coefficients, time/runtime half-lives, minimum `match_similarity`, and failure-blocking thresholds are currently fixed and recorded with the schema version in planning snapshots. A future versioned `EvidenceSimilarityPolicy` should be selectable by tenant or deployment policy. Allow only server-allowlisted presets validated through offline replay; record policy ID and all parameters. Arbitrary per-prompt or UI weight tuning is forbidden. Before becoming default, a new policy must be compared with the default on ranking stability, SLO hit rate, and missed negative evidence in historical replay.

If quantization or model architecture differs, $S_{model}=0$; otherwise:

$$
S_{model} = 0.45S_{family} + 0.25 + 0.20e^{-0.8|\ln(p_r/p_q)|} + 0.10S_{tokenizer}
$$

$S_{family}$ is $1$ for the same model family and $0.15$ otherwise; $S_{tokenizer}$ is $1$ for the same tokenizer and $0.5$ otherwise. For MoE, $p$ prefers active parameter count. Hardware similarity compares GPU count only:

$$
S_{hardware} = 0.7 + 0.3e^{-|\ln(g_r/g_q)|}
$$

$S_{workload}$ averages five log-ratio scores for mean input, P95 input, mean output, concurrency, and request rate, plus two $1-|x_r-x_q|$ scores for shared-prefix and prefill fractions. Values in $q$ come from the use-case mapping or explicit overrides above. Candidate-configuration similarity is:

$$
S_{configuration} = 0.4e^{-|\ln(TP_r/TP_q)|} + 0.4e^{-|\ln(R_r/R_q)|} + 0.2J_{optimization}
$$

$J_{optimization}$ is the Jaccard overlap of the two optimization sets; database hard filtering already ensures identical topology.

Explicit user SLOs take precedence. Where corresponding targets are absent, TTFT, TPOT, and workload limits from `UseCasePerformanceEstimate` serve as uncertainty-aware soft targets. For each required latency limit $L$ and historical measured latency $m_r$, compute:

$$
s_{latency} = e^{-2\max(0, \ln(m_r/L))}
$$

This term is $1$ when the target is met, $0.25$ at twice the target latency, and quickly approaches $0$ for large deviations. Apply the same one-sided penalty to required workload capacity $c_q$ and historically supported capacity $c_r$:

$$
s_{capacity} = e^{-2\max(0, \ln(c_q/c_r))}
$$

$$
S_{SLO} = \min(s_{TTFT}, s_{TPOT}, s_{capacity})
$$

Take the minimum only over targets present in the request. If history lacks a metric required by an explicit SLO, the record cannot be high-similarity performance evidence, though it may remain diagnostic. If estimated targets are too uncertain or metrics unavailable, do not fabricate $S_{SLO}$: downgrade the evidence state so a high score without performance targets cannot replace records satisfying explicit SLOs.

$S$ is base similarity: it describes the fit of a benchmark's model, hardware, workload, configuration, and measured performance to the current target.

The current implementation also computes time and runtime decay:

$$
W = S \times D_{time} \times D_{runtime}
$$

$W$ is **match similarity** in this document. It adds time/runtime effects to base similarity $S$, including SLO-performance fit, and expresses how relevant the historical record is to the current decision. AI scoring reads raw samples ordered by $W$. Deterministic fallback uses $W$ for weighted metric averages, effective sample size, deterministic `confidence`, failure blocking, and SLO/performance ranking. AI does not receive those aggregates. The current payload field remains named `match_confidence`, but carries $W$, defined here as `match_similarity`.

### Unstructured diagnostic evidence

Logs, traces, artifacts, and external materials become bounded `DiagnosticEvidenceProjection` objects with fixed `evidence_id`, content hash, source revision, tenant, ACL, and necessary runtime metadata. Before use, artifacts must pass source validation against their manifest and file SHA-256.

OpenSearch queries must apply tenant/ACL filters first. Use BM25 by default and kNN only when the caller explicitly supplies a vector. Projections are rebuildable. Unavailable OpenSearch, stale indexes, or no results yield empty features/diagnostics without affecting deterministic planning.

### Two-stage retrieval of cloud benchmark reports

A future controlled importer will read historical reports from configured cloud locations periodically or on events, creating OpenSearch RAG projections for discovery only. It must validate source identity, tenant/ACL, report schema version, content hash, collection time, and artifact manifest. Deduplicate idempotently using stable source report ID and hash. Unverifiable reports get no projection; incompletely normalizable reports are explanatory only, not performance-planning inputs.

Stage one retrieves report projections with tenant/ACL filtering to locate potentially relevant originals. BM25/kNN hits indicate textual relevance, not benchmark comparability, `match_similarity`, or performance conclusions. External reports are not written into `agentic_benchmark_records`; projections retain only cloud URI, producer/schema version, hash, source report ID, and controlled metadata needed to fetch the original.

Stage two processes only retrieved reports. The server revalidates the source manifest/hash, ACL, and schema, then temporarily normalizes fully parseable reports to the same `BenchmarkRecord` shape as online collection within this planning request: model, accelerator/runtime, workload, candidate configuration, TTFT/TPOT/throughput, outcome, and failure reason. Temporary records are not persisted. Missing performance-comparison fields restrict a report to diagnosis/explanation; it cannot score candidates.

Temporary records passing stage two and local persisted records undergo identical hard filters and $S \rightarrow W$ computation, yielding `match_similarity` with the same meaning. For each hard-valid candidate, the raw-sample view gives AI scoring at most five benchmark evidence samples ordered together by descending $W$, each containing `match_similarity`, metrics, outcome, failure reason, and traceable source ref. There are no separate source quotas.

## Two consumption paths

### Deterministic path

The deterministic path becomes the final decision path only when AI is unconfigured, generation/scoring fails, or the response is invalid. For each hard-valid candidate it computes $D_{time}$, $D_{runtime}$, and $W$ over all matched samples, then builds `PerformanceScoreInput`.

Aggregated history has only two roles:

1. If sufficiently many sufficiently similar failures exist, set `has_blocking_negative_evidence` and exclude the candidate to avoid recommending a known failing configuration again.
2. If successful history is numerous and similar enough to support stable performance conclusions, compare requested SLO compliance first; ties compare deterministic `confidence`, effective sample size, TTFT, TPOT, throughput, and error rate in that order.

Too few or insufficiently similar samples, missing metrics, or unresolved failure evidence do not imply a performance advantage or disadvantage. Rank that candidate using exact AIC matches and existing heuristics instead.

This path does not consume unstructured OpenSearch content and does not let AI rewrite failure blocking, SLO conclusions, or ranking.

### AI path

AI generation and scoring are separate stages. The generator first retrieves historical benchmarks matching the model, available resources, and requested SLOs. Only verified candidate configurations and their controlled provenance may enter the generator prompt as candidate priors. Without qualifying history, omit priors and generate proposals from normal inputs. Every proposal must pass `CandidateValidator`.

For each feasible provider, AI Generator must first propose the minimum-resource topology: the lowest TP satisfying memory constraints and one replica per serving role. Explicit provider preference selects a method; it cannot independently increase TP or replicas. Additional resources require explicit SLOs or complete AIC predictions. Scaling from a relaxed-search anchor remains an estimate requiring validation.

AI scoring first shortlists at most ten hard-valid candidates: retain the best candidate per provider in deterministic order, then fill remaining places by rank. Only shortlisted candidates reach AI, each with at most five real historical benchmarks ordered by similarity. Raw benchmarks describe measured historical behavior: AI may compare TTFT, TPOT, throughput, outcome, and failure reason, for example avoiding candidates that previously caused OOM under similar workloads. ACL-filtered, bounded, version-pinned Guide/GitHub/paper feature packets are not yet integrated into AI scoring. Future packets may inform method prerequisites, version compatibility, and operator preference only; they are not measurements and cannot establish TTFT, TPOT, throughput, SLOs, or confidence.

AI cannot generate new candidates or interpret unstructured features as performance measurements. Responses may select only server-provided candidate IDs. Invalid, timed-out, or unavailable responses fall back to the deterministic path.

### Generator prompt construction and example

Generation is a tool-calling stage, not a directly deployable configuration catalog. The first server-built user message contains only `cluster_id`, `model`, `operator_preference`, and `deployment_intent` stripped of live-resource/AIC/evidence fields. The system prompt fixes the provider allowlist/semantics, minimum-resource topology principle, PD field contract, and read-only MCP allowlist. It forbids inventing providers, hardware, AIC results, or benchmark facts, and forbids writes, deployment, approval, deletion, and port-forward operations.

The first round exposes only `get_cluster_overview` and `search_candidates`, requiring parallel calls; `sourceIds` must include `aic`. The server normalizes arguments using `cluster_id` and `sourceIds` and returns bounded results. Later calls remain restricted to allowlisted read-only tools. The model submits one to ten proposals only through `submit_candidate_proposals`; `CandidateValidator` independently recomputes memory, GPU, CPU buffer, PD completeness, and deduplication. The current implementation injects no historical benchmark prior. Future priors must be a separate bounded field containing validated candidates/provenance, without aggregate metrics, confidence, $N_{eff}$, SLO conclusions, or unauthorized data.

Illustrative first user message, without real credentials, tool results, or full internal facts:

```json
{
   "cluster_id": "cluster-a",
   "model": "meta-llama/Llama-3.1-8B-Instruct",
   "operator_preference": "Prioritize low latency; PD is allowed.",
   "deployment_intent": {
      "context_length": 8192,
      "model_weight_gib": 16.0,
      "ttft_slo_ms": 500,
      "tpot_slo_ms": 35,
      "workload": {
         "mean_input_tokens": 2048,
         "mean_output_tokens": 256,
         "prefill_heavy": true
      }
   }
}
```

After required MCP calls, the model may submit only through a schema-constrained tool call, for example:

```json
{
   "candidates": [
      {
         "provider_ref": "pd-disaggregation",
         "replicas": 1,
         "tensor_parallel_size": 2,
         "prefill_replicas": 1,
         "prefill_tensor_parallel_size": 2,
         "rationale": "Validated cluster capacity and a long-input workload support separating prefill and decode.",
         "evidence_ids": ["cluster-overview:cluster-a", "aic:search"]
      }
   ]
}
```

This example promises neither resource availability nor performance. `aic:search` may be cited only when AIC returns a complete prediction satisfying every explicit TTFT/TPOT SLO. The validator may reject any proposal.

### Scoring prompt construction and example

Scoring is a single selection-only call. The server removes aggregate internal fields such as `planning_facts.performance_score_inputs`, then builds JSON context from `planning_facts`, `valid_candidates` with `deployable=true`, and optional `operator_preference`. Each candidate contains only validated topology, required GPUs, guide, exactly matched AIC `performance_estimate`, and at most five `historical_benchmarks` ordered by descending `match_similarity`. Explicit `not_applicable` AIC status must not reduce rank; a missing prediction is not zero. Aggregate `PerformanceScoreInput`, confidence, $N_{eff}$, blocking failures, deterministic rank, and internal SLO conclusions never reach the scorer.

The system prompt requires selection only from `valid_candidates`, respecting hard constraints first and treating operator preference as the primary scoring condition, informed by TTFT, TPOT, throughput, resources, AIC, and immutable history. It explains that `match_confidence` is historical relevance, not success probability or a performance guarantee. The model cannot rewrite metrics, outcome, configuration, or that score. Responses must satisfy the JSON schema for `candidate_id`, `candidate_ids`, `scores`, `confidence`, and `rationale`: return three candidates (all if fewer than three), scores in $[0, 1]$, and a highest-scoring provided ID as `candidate_id`. The server validates ID/score sets and highest-score consistency; failure uses deterministic fallback.

Example candidate in scoring context: historical values are measurements. The current `match_confidence` expresses configuration, workload, time, and runtime relevance without aggregate SLO conclusions. Incorporating SLO-performance fit into `match_similarity` is defined here but remains future implementation work:

```json
{
   "id": "pd-disaggregation-p1-tp2-d1-tp2",
   "provider_ref": "pd-disaggregation",
   "replicas": 1,
   "tensor_parallel_size": 2,
   "prefill_replicas": 1,
   "prefill_tensor_parallel_size": 2,
   "required_gpus": 4,
   "performance_estimate_applicability": "applicable",
   "performance_estimate": {
      "ttft_ms": 420,
      "tpot_ms": 28,
      "throughput_tokens_per_sec": 760
   },
   "historical_benchmarks": [
      {
         "benchmark_id": "benchmark-20260918-042",
         "match_confidence": 0.91,
         "outcome": "succeeded",
         "ttft_p95_ms": 430,
         "tpot_p95_ms": 30,
         "throughput_tokens_per_s": 735,
         "failure_reason": null
      },
      {
         "benchmark_id": "benchmark-20260912-007",
         "match_confidence": 0.74,
         "outcome": "failed",
         "failure_reason": "OOM"
      }
   ]
}
```

Minimal valid response example: `rationale` explains decisive differences without repeating every parameter or fabricating performance facts:

```json
{
   "candidate_id": "pd-disaggregation-p1-tp2-d1-tp2",
   "candidate_ids": [
      "pd-disaggregation-p1-tp2-d1-tp2",
      "optimized-baseline-tp2-r1",
      "baseline-vllm-tp2-r1"
   ],
   "scores": {
      "pd-disaggregation-p1-tp2-d1-tp2": 0.91,
      "optimized-baseline-tp2-r1": 0.79,
      "baseline-vllm-tp2-r1": 0.68
   },
   "confidence": 0.82,
   "rationale": "The preferred candidate fits the low-latency preference and has comparable AIC and historical measurements; the others are validated alternatives."
}
```

### Use-case performance prediction inputs

At planning start, the server supplies the user use case, resolved workload, model metadata, and accelerator specifications to a versioned estimator for TTFT, TPOT, and workload limits such as supported concurrency/request rate. `UseCasePerformanceEstimate` must include estimator version, input refs, applicability, and uncertainty. Insufficient or out-of-domain input returns `unavailable`, not fabricated values.

Store the estimate separately from explicit SLOs, which always take precedence; estimates are soft targets only where corresponding targets are absent. Pass them to the generator to choose minimum-resource topology for expected workload, to the retriever for $S_{SLO}$, and to deterministic scoring for TTFT/TPOT/capacity comparison against candidate-level benchmark/AIC evidence. Estimates cannot override live hard constraints, legalize rejected candidates, or substitute for measured benchmarks; excessive uncertainty cannot produce high-similarity evidence. AI scoring receives only a validated estimate summary with uncertainty and cannot describe it as measured performance.

### Unstructured features and diagnosis

The server builds AI feature packets only from version-pinned, ACL-filtered, length-bounded Guide, GitHub, and paper summaries. AI may use them for method applicability, version compatibility, and preference tradeoffs among existing candidates; it cannot convert them into performance measurements, SLOs, confidence, candidate rejection, or new configurations.

Logs, traces, artifacts, and snapshots remain diagnostic. All unstructured evidence only supplements explanations for existing candidates; it cannot generate candidates or affect the Deterministic Planner's numeric performance decisions.

## Current implementation and next steps

### Implemented

- Historical-to-candidate similarity computes base $S$ and time/runtime decay to produce `match_similarity` $W$. AI scoring selects and orders bounded raw history per candidate using $W$.
- Historical benchmarks can be appended and filtered by current deployment conditions. Repeated failures or sufficiently supported stable performance conclusions inform deterministic fallback.
- AI scoring receives bounded raw history during plan creation; unavailable or invalid AI results use separate aggregated-history deterministic decisions.
- Candidate priors can be extracted from fully validated history, but do not affect generation by default. Enabling them first observes or validates before admitting candidates.
- Traceable, redacted planning snapshots are saved, and resources are checked again before approval.
- Historical candidate seeds default off. Enabling `AGENTIC_BENCHMARK_SEEDS_ENABLED` defaults to `shadow`: record qualifying seed counts without merging. Only `AGENTIC_HISTORICAL_SEEDS_MODE=apply` admits seeds after `CandidateValidator`. Nullable prefill parameters can participate in ranking without comparing `None` against integers and interrupting planning.
- Snapshots separately save the selected candidate and its `evidence`, planning evidence, and `decision_evidence_ids`. The latter deduplicates cluster overview, applicable AIC refs, selected-candidate evidence refs, and historical sample IDs; it excludes unselected candidates. AI scoring scope is not the entire hard-valid catalog.
- Diagnostic RAG foundations support searchable logs/traces/artifacts with access controls and artifact-integrity checks. This RAG is not yet integrated into deployment planning or AI scoring and cannot affect candidate generation, selection, or performance decisions.

### Incomplete work and next steps

1. Apply the same rule to plan creation and refinement: AI reads only raw history for its hard-valid shortlist; aggregation, failure blocking, and ranking remain in deterministic fallback.
2. Integrate complete actual workload into benchmark retrieval. Missing workload means unavailable historical evidence, not invented performance.
3. Add controlled Guide/GitHub/paper summaries to AI scoring with fixed versions, access scope, and length. Query failure omits those features without affecting other decisions.
4. Complete default-off historical priors with validated configuration/provenance only, no aggregate conclusions; record whether each plan used a prior.
5. Before cross-tenant benchmark queries, define ownership/authorization and verify tenant isolation.
6. After access boundaries are clear, expose logs/traces/artifacts for explanation only, not AI recommendation or deterministic performance ranking.
7. Record policies/switches in snapshots and replay history to compare deterministic decisions before/after priors.
8. Archive candidates, final selection, and rationale for each AI/deterministic decision. With redaction, access controls, and pinned versions, index snapshots as RAG references for similar-decision context/explanation; never treat them as measurements or alter deterministic ranking.
9. Implement two-stage cloud-report retrieval: discovery-only RAG projections, then source validation and request-local `BenchmarkRecord` normalization. Do not persist records; use the same `match_similarity` as online records to supply at most five jointly ranked raw samples per candidate.
10. Implement/replay a versioned `EvidenceSimilarityPolicy` with configurable weights, decay, and thresholds, forbidding arbitrary per-request tuning.
11. Implement the use-case + accelerator/model estimator, passing uncertainty-aware TTFT, TPOT, and workload limits to generator/scorer as soft targets when explicit SLOs are absent. Verify it cannot bypass hard constraints or replace measurements.