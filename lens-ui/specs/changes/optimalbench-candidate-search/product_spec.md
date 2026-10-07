# OptimalBench Candidate Search

## Goal

Replace synthesized Predictive and Search-based candidates in Prism's Search Candidates stage with configurations produced by OptimalBench.

## Requirements

- Predictive uses OptimalBench AIConfigurator search and returns its ranked aggregated, P/D, or E/P/D topologies with predicted TTFT, TPOT, and throughput.
- Search-based exposes the Compare Search strategies: optimized baseline, P/D disaggregation, E/P/D disaggregation, and tiered prefix cache.
- The workload model, ISL, OSL, TTFT SLA, and TPOT SLA are passed to OptimalBench.
- Users configure the accelerator budget, AIC system profile, AIC backend, and per-mode result limit in Prism.
- Existing/historical and manual sources remain available.
- Upstream failures are visible and never silently replaced by mock predictive/search results.

## Acceptance criteria

1. Selecting AIC Prediction calls OptimalBench `/api/predict/search` through the Prism backend.
2. Selecting a Compare Search strategy returns actual OptimalBench-generated topology configs.
3. Aggregated and P/D candidates can flow into the existing deployment plan contract.
4. E/P/D candidates retain encode topology and are clearly marked as not deployable by the current deployment backend.
5. Candidate source, rank/config ID, topology, GPU count, and available predictions are displayed.
