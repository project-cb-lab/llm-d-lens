"""Unit tests for in-tree capacity planning and deployment validation engine."""

import pytest

from llm_d_bench.capacity import (
    AttentionType,
    ConfigWrapper,
    KVCacheDetail,
    ValidationParams,
    allocatable_kv_cache_memory,
    auto_max_model_len,
    check_model_fits_gpu,
    estimate_vllm_activation_memory,
    estimate_vllm_non_torch_memory,
    evaluate_capacity,
    find_possible_tp,
    max_concurrent_requests,
    max_context_len,
    model_memory_req,
    precision_to_byte,
    total_kv_cache_blocks,
    validate_vllm_params,
    wrap_config,
)
from llm_d_bench.capacity.vllm_constants import (
    ACTIVATION_PROFILES,
    VLLM_NON_TORCH_MEMORY_TP1_GIB,
    VLLM_NON_TORCH_MEMORY_TPN_GIB,
)


@pytest.fixture
def llama3_8b_config() -> dict:
    """Mock config.json dictionary representing meta-llama/Llama-3-8B."""
    return {
        "architectures": ["LlamaForCausalLM"],
        "hidden_size": 4096,
        "num_hidden_layers": 32,
        "num_attention_heads": 32,
        "num_key_value_heads": 8,
        "intermediate_size": 14336,
        "vocab_size": 128256,
        "max_position_embeddings": 8192,
        "torch_dtype": "bfloat16",
    }


@pytest.fixture
def qwen2_72b_config() -> dict:
    """Mock config.json dictionary representing Qwen/Qwen2-72B."""
    return {
        "architectures": ["Qwen2ForCausalLM"],
        "hidden_size": 8192,
        "num_hidden_layers": 80,
        "num_attention_heads": 64,
        "num_key_value_heads": 8,
        "intermediate_size": 29568,
        "vocab_size": 152064,
        "max_position_embeddings": 32768,
        "torch_dtype": "bfloat16",
    }


@pytest.fixture
def deepseek_v2_mla_config() -> dict:
    """Mock config.json dictionary representing DeepSeek-V2 with MLA."""
    return {
        "architectures": ["DeepseekV2ForCausalLM"],
        "hidden_size": 5120,
        "num_hidden_layers": 60,
        "num_attention_heads": 128,
        "num_key_value_heads": 128,
        "kv_lora_rank": 512,
        "qk_rope_head_dim": 64,
        "vocab_size": 102400,
        "max_position_embeddings": 4096,
        "torch_dtype": "bfloat16",
    }


def test_vllm_constants():
    assert "LlamaForCausalLM" in ACTIVATION_PROFILES
    assert ACTIVATION_PROFILES["LlamaForCausalLM"] == 1.89
    assert ACTIVATION_PROFILES["Qwen2ForCausalLM"] == 2.25
    assert VLLM_NON_TORCH_MEMORY_TP1_GIB > 0
    assert VLLM_NON_TORCH_MEMORY_TPN_GIB > VLLM_NON_TORCH_MEMORY_TP1_GIB


def test_precision_to_byte():
    assert precision_to_byte("bf16") == 2.0
    assert precision_to_byte("float16") == 2.0
    assert precision_to_byte("fp8") == 1.0
    assert precision_to_byte("int4") == 0.5
    assert precision_to_byte("mxfp4") == 4.25 / 8.0
    with pytest.raises(ValueError):
        precision_to_byte("unknown-invalid-dtype")


def test_config_wrapper(llama3_8b_config):
    cfg = wrap_config(llama3_8b_config)
    assert cfg.hidden_size == 4096
    assert cfg.get("num_attention_heads") == 32
    assert "num_key_value_heads" in cfg


def test_find_possible_tp_for_llama(llama3_8b_config):
    valid_tp = find_possible_tp(llama3_8b_config)
    # Llama 3 8B: 32 heads, 8 kv heads. Valid TP: 1, 2, 4, 8, 16, 32
    # But intermediate_size = 14336: 14336 / 16 = 896, 14336 / 32 = 448
    # padded_vocab_size = 128256 (divisible by 64, 128256 / 32 = 4008)
    assert 1 in valid_tp
    assert 2 in valid_tp
    assert 4 in valid_tp
    assert 8 in valid_tp
    # Arbitrary non-divisible numbers must not be present
    assert 3 not in valid_tp
    assert 5 not in valid_tp
    assert 7 not in valid_tp


def test_kv_cache_detail_gqa(llama3_8b_config):
    kv = KVCacheDetail("meta-llama/Llama-3-8B", llama3_8b_config, context_len=4096, batch_size=1)
    assert kv.attention_type == AttentionType.GQA
    # head_dim = 4096 / 32 = 128
    assert kv.head_dimension == 128
    # per_token_bytes = 32 layers * 2 (K,V) * 128 (head_dim) * 8 (kv_heads) * 2 bytes (bf16)
    expected_per_token = 32 * 2 * 128 * 8 * 2
    assert kv.per_token_memory_bytes == expected_per_token
    assert kv.per_request_kv_cache_bytes == expected_per_token * 4096
    assert kv.per_request_kv_cache_gb > 0.4  # ~0.5 GiB for 4K context


def test_kv_cache_detail_mla(deepseek_v2_mla_config):
    kv = KVCacheDetail("deepseek-ai/DeepSeek-V2", deepseek_v2_mla_config, context_len=4096, batch_size=1)
    assert kv.attention_type == AttentionType.MLA
    # per_token_bytes = 60 layers * (512 + 64) * 2 bytes
    expected_per_token = 60 * (512 + 64) * 2
    assert kv.per_token_memory_bytes == expected_per_token


def test_estimate_vllm_activation_memory(llama3_8b_config):
    assert estimate_vllm_activation_memory(llama3_8b_config, tp=1) == 1.89
    # Unknown dense model fallback
    generic_dense = {"architectures": ["SomeNewDenseModel"]}
    assert estimate_vllm_activation_memory(generic_dense, tp=1) == 2.00
    # MoE model
    moe_config = {"architectures": ["CustomMoE"], "num_experts": 8}
    assert estimate_vllm_activation_memory(moe_config, tp=1) == 2.50


def test_allocatable_kv_cache_and_concurrency(llama3_8b_config):
    # Model size ~ 15.0 GiB (8B * 2 bytes / 1024^3 ~ 14.9 GiB)
    # On 80GB GPU at TP=1, util=0.9 -> 72 GiB available.
    # Consumed: ~15 (weights) + 1.89 (act) + 0.27 (non-torch) ~ 17.16 GiB.
    # Allocatable KV ~ 72 - 17.16 ~ 54.8 GiB.
    allocatable = allocatable_kv_cache_memory(
        "meta-llama/Llama-3-8B",
        llama3_8b_config,
        gpu_memory=80,
        gpu_util=0.9,
        tp=1,
        fallback_weight_gib=15.0,
    )
    assert allocatable > 50.0

    concurrency = max_concurrent_requests(
        "meta-llama/Llama-3-8B",
        llama3_8b_config,
        max_model_len=4096,
        gpu_memory=80,
        gpu_util=0.9,
        tp=1,
        fallback_weight_gib=15.0,
    )
    assert concurrency > 50

    # Test OOM scenario: 70B model on single 16GB GPU
    oom_allocatable = allocatable_kv_cache_memory(
        "meta-llama/Llama-3-70B",
        llama3_8b_config,
        gpu_memory=16,
        gpu_util=0.9,
        tp=1,
        fallback_weight_gib=140.0,
    )
    assert oom_allocatable < 0  # Definite OOM!

    fits = check_model_fits_gpu(
        "meta-llama/Llama-3-8B",
        llama3_8b_config,
        gpu_memory_gb=80,
        fallback_weight_gib=15.0,
    )
    assert 1 in fits


def test_auto_max_model_len_and_blocks(llama3_8b_config):
    max_len = auto_max_model_len(
        "meta-llama/Llama-3-8B",
        llama3_8b_config,
        gpu_memory=80,
        gpu_mem_util=0.9,
        tp=1,
        fallback_weight_gib=15.0,
    )
    # Model max is 8192, GPU has 50GB KV, so it easily hits 8192 cap
    assert max_len == 8192

    blocks = total_kv_cache_blocks(
        "meta-llama/Llama-3-8B",
        llama3_8b_config,
        context_len=4096,
        gpu_memory=80,
        tp=1,
        fallback_weight_gib=15.0,
    )
    assert blocks > 1000


def test_validate_vllm_params_diagnostics(llama3_8b_config):
    # Case 1: Valid deployment
    valid_params = ValidationParams(
        models=["meta-llama/Llama-3-8B"],
        gpu_memory=80,
        tp=1,
        max_model_len=4096,
        model_config=llama3_8b_config,
        fallback_weight_gib=15.0,
    )
    msgs = validate_vllm_params(valid_params)
    assert not any("DEPLOYMENT WILL FAIL" in m for m in msgs)
    assert not any("is invalid for" in m for m in msgs)

    # Case 2: Invalid TP
    invalid_tp_params = ValidationParams(
        models=["meta-llama/Llama-3-8B"],
        gpu_memory=80,
        tp=3,  # 3 is invalid for 32 heads
        max_model_len=4096,
        model_config=llama3_8b_config,
        fallback_weight_gib=15.0,
    )
    msgs = validate_vllm_params(invalid_tp_params)
    assert any("TP=3 is invalid for" in m for m in msgs)

    # Case 3: OOM
    oom_params = ValidationParams(
        models=["meta-llama/Llama-3-8B"],
        gpu_memory=16,  # 16 GB GPU cannot hold 15GB weights + activation + non-torch + KV
        tp=1,
        max_model_len=4096,
        model_config=llama3_8b_config,
        fallback_weight_gib=15.0,
    )
    msgs = validate_vllm_params(oom_params)
    assert any("DEPLOYMENT WILL FAIL" in m for m in msgs)
    assert any("Suggestion: Increase Tensor Parallelism" in m for m in msgs)


def test_evaluate_capacity(llama3_8b_config):
    params = ValidationParams(
        models=["meta-llama/Llama-3-8B"],
        gpu_memory=80,
        tp=2,
        max_model_len=4096,
        model_config=llama3_8b_config,
        fallback_weight_gib=15.0,
    )
    result = evaluate_capacity(params)
    assert result.is_deployable is True
    assert result.allocatable_kv_cache_gib > 0
    assert result.max_concurrent_requests > 0
    assert "model_weights_gib" in result.memory_breakdown
    assert "activation_memory_gib" in result.memory_breakdown


def test_estimate_capacity_router(llama3_8b_config, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from llm_d_bench.capacity.router import router

    monkeypatch.setattr(
        "llm_d_bench.capacity.validator.load_model_config",
        lambda model_name, hf_token=None, local_path=None: llama3_8b_config,
    )
    monkeypatch.setattr(
        "llm_d_bench.capacity.capacity_planner.load_model_config",
        lambda model_name, hf_token=None, local_path=None: llama3_8b_config,
    )
    test_app = FastAPI()
    test_app.include_router(router)
    client = TestClient(test_app)

    payload = {
        "model": "meta-llama/Llama-3-8B",
        "gpu_memory_gib": 80.0,
        "tensor_parallel_size": 1,
        "max_model_len": 4096,
        "gpu_memory_utilization": 0.9,
    }
    resp = client.post("/api/v1/capacity/estimate", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_deployable"] is True
    assert data["allocatable_kv_cache_gib"] > 0
    assert data["max_concurrent_requests"] > 0
    assert "model_weights_gib" in data["memory_breakdown"]
    assert 1 in data["possible_tps"]
