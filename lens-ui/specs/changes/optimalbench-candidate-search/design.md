# Engineering design

## Architecture

Lens's Express server provides `POST /api/candidate-search`. It uses a fixed, operator-configured `OPTIMALBENCH_API_URL` (default `http://127.0.0.1:8080`) and never accepts an upstream URL from the browser.

For Predictive, the BFF calls `POST /api/predict/search` and normalizes AIC results into Lens `CandidateConfig` objects.

For Search-based modes, the BFF calls `GET /api/pd-search/preview-configs` for baseline, P/D, and E/P/D spaces. If a deployed OptimalBench version predates E/P/D preview fields, the BFF uses a direct port of OptimalBench `_generate_epd_search_space` with the same defaults and GPU constraint. Tiered Prefix Cache candidates are shape-aligned clones of OptimalBench baseline configs, matching OptimalBench `_clone_baseline_configs_for_tpc` behavior. Results are deterministically sorted by GPU utilization and capped per selected mode.

Existing/User candidates are produced locally by the frontend until Results Store candidate lookup is specified.

## Candidate contract

Each candidate has `id`, `name`, `source`, `sourceLabel`, `family`, topology fields, `totalGpus`, optional `predicted`, optional `confidence`, `topologyMode`, and optional `deployable`/`deploymentNote`. E/P/D adds `encodeTp` and `encodeReplicas`.

## Failure handling

- The BFF enforces a request timeout and propagates sanitized OptimalBench errors.
- The frontend resets running state in `finally`, displays errors, and does not synthesize replacement metrics.

## Configuration

`OPTIMALBENCH_API_URL` selects the OptimalBench API origin. No database schema changes are required.
