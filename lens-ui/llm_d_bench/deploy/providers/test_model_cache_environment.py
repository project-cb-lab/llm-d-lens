"""Tests for the cache-backed serving Pod Hugging Face environment."""

from llm_d_bench.deploy.providers.model_cache_environment import model_cache_environment


def test_auto_cache_points_hf_home_at_the_mount_and_goes_offline():
    assert model_cache_environment("auto-cache") == {
        "HF_HOME": "/model-cache",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }


def test_shared_path_without_a_mount_is_unchanged():
    assert model_cache_environment("shared-path") == {}


def test_any_cache_mounted_source_goes_offline_without_hf_home():
    # shared-path and ad-hoc mountPath providers serve the mounted directory as a
    # local path, so only the offline flags apply.
    assert model_cache_environment("shared-path", cache_mounted=True) == {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    assert model_cache_environment("huggingface", cache_mounted=True) == {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }


def test_huggingface_source_stays_online():
    assert model_cache_environment("huggingface") == {}


def test_custom_mount_path_is_honored():
    assert model_cache_environment("auto-cache", mount_path="/data/hf")["HF_HOME"] == "/data/hf"
