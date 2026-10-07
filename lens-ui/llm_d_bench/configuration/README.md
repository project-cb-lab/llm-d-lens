# Prism Configuration API

FastAPI implementation of the configuration contract in [specs/changes/aiconfiguration.md](../../specs/changes/aiconfiguration.md).

## Run locally

From the repository root, in an environment containing the packages in `requirements.txt`:

```sh
python -m uvicorn llm_d_bench.api.main:app --host 127.0.0.1 --port 8090
```

The backend follows the same module boundary as Prism's Simulation workflow:

- `llm_d_bench.api.main` owns the minimal FastAPI application and health endpoint.
- `configuration.router` owns HTTP request handling and error translation.
- `configuration.models` defines API contracts.
- `configuration.service` owns resolve, render, and persistence orchestration.
- `configuration.normalizers`, `validators`, and `capability` implement domain operations.

`llm_d_bench.configuration.app:app` remains available as a compatibility import.

Prism's Express server proxies `/api/configurations/{resolve,render,save}` to this service. Override the upstream with `CONFIGURATION_API_URL`; set the data root with `LENS_DATA_DIR` (configurations use its `artifacts/configurations` subdirectory) (the default is `~/.local/share/lens/artifacts/configurations`). The logical file path returned to Deploy is `/configs/<artifact-id>/<file>`.

The resolve operation calls the in-process AIConfigurator adapter in `llm_d_bench.aic` before normalization and validation. The adapter uses the pinned `aiconfigurator` nightly wheel and does not require a separate prediction service.

## Evaluation configuration integrity

The planner renders the selected Guide, applies explicit controls, and captures
the router inputs from the same resolved source. Render and save verify the
declared model, role topology, image, arguments, environment and storage against
the actual model-server resources. The deployment adapter repeats these checks;
recomputing a checksum cannot make contradictory configuration facts valid.
Unrecognized or compound shell invocations are rejected rather than rewritten.

`candidate_config.guide_settings` persists as `content.guideSettings`:

- `cacheCpuGiB`: CPU cache capacity per pod for Native CPU or LMCache CPU
  offload. Native capacity updates `cpu_bytes_to_use`; LMCache uses
  `LMCACHE_MAX_LOCAL_CPU_SIZE`. File-based LMCache settings require a compatible
  Guide source because an environment override cannot be assumed to win.
- `rdmaNicCount`: positive NIC count per pod for the P/D `vllm-rdma` variant.
  GPU TP, NIC counts and device selectors are independent.
- `routerValues`: YAML mapping merged over the Guide's Helm values. Precise
  routing binds tokenizer model identity and index block size to the model
  servers, and requires one EPP replica for its token-load accounting.

`render.deployment_bundle` persists as `officialGuide.deploymentBundle`. The
bundle records the resolved source commit, Helm chart/version, original and
effective router values, auxiliary resources, and calibration recipe where
required. Every embedded asset has a SHA-256 checksum. Deployment uses saved
inputs and records effective router values and installed manifest evidence;
calibration is recorded separately from the immutable requested configuration.

`GET /api/v1/configurations/artifacts/{artifact_id}/bundle` downloads these inputs
and the model-server YAML as a ZIP. The Evaluation YAML dialog provides the
download link. Older artifacts without a bundle must be regenerated to obtain
complete inputs. The standalone model-server YAML is not the entire Guide.

Official and remote sources must include the selected Guide's router recipes;
local sources must reside in a complete Guide checkout for those recipes to be
captured. Missing auxiliary sources fail generation instead of falling back to
an unrelated mutable local checkout. Filesystem offload remains unsupported.

LMCache option reference: [CPU RAM configuration](https://docs.lmcache.ai/kv_cache/storage_backends/cpu_ram.html).
