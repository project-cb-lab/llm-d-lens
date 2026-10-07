"""Environment for model-serving Pods that read a model from the mounted cache.

The Model Cache download Job writes the standard Hugging Face layout under
``HF_HOME=/model-cache`` (see ``llm_d_bench/model_cache/jobs.py``) and
cache-backed serving Pods mount the same volume at ``/model-cache``
(``storage_mount.resolve_mount``). In the ``auto-cache`` source the Pod is handed
the model repository id, so vLLM resolves it through ``HF_HOME``; left alone it
still contacts ``huggingface.co`` to list repository files at startup, which
fails on restricted or flaky networks (for example ``repo_utils.py`` SSL
``unexpected_eof_while_reading`` errors) even though every file is already
cached.

Every cache-backed source runs Hugging Face offline so startup never depends on
outbound Hub access:

- ``auto-cache`` mounts an HF cache root and serves a repository id, so it also
  gets ``HF_HOME`` and both offline flags.
- ``shared-path`` and the ad-hoc ``mountPath`` sources mount the model directory
  itself and are handed a local path, so the offline flags are added without
  ``HF_HOME``.
- a deployment with no cache mount (``huggingface``) deliberately downloads at
  startup and stays online.
"""

from __future__ import annotations

MODEL_CACHE_MOUNT_PATH = "/model-cache"

_HF_OFFLINE_ENVIRONMENT = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}


def model_cache_environment(
    model_source: str, *, mount_path: str = MODEL_CACHE_MOUNT_PATH, cache_mounted: bool = False
) -> dict[str, str]:
    """Return the Hugging Face environment for a model-serving Pod.

    ``auto-cache`` always points ``HF_HOME`` at the mounted cache and goes
    offline. Any other source goes offline only when ``cache_mounted`` is true
    (a read-only ``shared-path`` volume or an ad-hoc ``mountPath``), because the
    Pod then serves the mounted directory locally. A source with no cache mount
    returns an empty mapping so it keeps resolving online.
    """
    if model_source == "auto-cache":
        return {"HF_HOME": mount_path, **_HF_OFFLINE_ENVIRONMENT}
    if cache_mounted:
        return dict(_HF_OFFLINE_ENVIRONMENT)
    return {}
