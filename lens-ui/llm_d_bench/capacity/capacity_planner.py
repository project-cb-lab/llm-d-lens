"""Capacity planner for LLM inference memory estimation and deployment sizing.

Implements exact memory estimation formulas for LLM inference with vLLM:
- Model weight memory requirements across precisions (FP16/BF16, FP8, INT4, MXFP4)
- KV cache memory for standard and advanced attention mechanisms (MHA, GQA, MQA, MLA)
- Peak activation memory during forward pass (calibrated per architecture)
- CUDA graph and non-torch communication/runtime overhead
- Sizing of allocatable KV cache, valid Tensor Parallelism (TP), and concurrency bounds

Zero external dependencies required; optionally leverages `transformers` and
`huggingface_hub` when installed for remote config/metadata resolution.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from .vllm_constants import (
    ACTIVATION_MEMORY_BASE_DENSE_GIB,
    ACTIVATION_MEMORY_BASE_MOE_GIB,
    ACTIVATION_MEMORY_BASE_MULTIMODAL_GIB,
    ACTIVATION_PROFILES,
    BYTES_PER_GIB,
    DEFAULT_KV_CACHE_DTYPE_BYTES,
    FP16_BF16_BYTES,
    MULTIMODAL_ARCHITECTURES,
    VLLM_NON_TORCH_MEMORY_TP1_GIB,
    VLLM_NON_TORCH_MEMORY_TP1_PP1_GIB,
    VLLM_NON_TORCH_MEMORY_TP1_PPN_GIB,
    VLLM_NON_TORCH_MEMORY_TPN_GIB,
)

logger = logging.getLogger(__name__)

# Optional dependencies
try:
    from huggingface_hub import HfApi
    from huggingface_hub.hf_api import ModelInfo, SafetensorsRepoMetadata

    _HF_AVAILABLE = True
except ImportError:
    _HF_AVAILABLE = False
    HfApi = None  # type: ignore[assignment,misc]
    ModelInfo = None  # type: ignore[assignment,misc]
    SafetensorsRepoMetadata = None  # type: ignore[assignment,misc]

try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from transformers import AutoConfig

    _TRANSFORMERS_AVAILABLE = True
except ImportError:
    _TRANSFORMERS_AVAILABLE = False
    AutoConfig = None  # type: ignore[assignment,misc]

# vLLM pads vocab_size to a multiple of 64 before sharding across TP ranks.
_VLLM_VOCAB_PADDING = 64


class AttentionType(StrEnum):
    """Attention mechanism types supported by the capacity planner."""

    MLA = "Multi-head latent attention"
    MHA = "Multi-head attention"
    GQA = "Grouped-query attention"
    MQA = "Multi-query attention"


class ConfigWrapper:
    """Unified read-only access for dicts, SimpleNamespaces, and AutoConfig objects."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(self._raw, dict):
            return self._raw.get(key, default)
        return getattr(self._raw, key, default)

    def __getattr__(self, item: str) -> Any:
        if isinstance(self._raw, dict):
            if item in self._raw:
                val = self._raw[item]
                if isinstance(val, dict):
                    return ConfigWrapper(val)
                return val
            raise AttributeError(f"Config has no attribute {item}")
        val = getattr(self._raw, item)
        if isinstance(val, dict):
            return ConfigWrapper(val)
        return val

    def __contains__(self, item: str) -> bool:
        if isinstance(self._raw, dict):
            return item in self._raw
        return hasattr(self._raw, item)

    @property
    def raw(self) -> Any:
        return self._raw


def wrap_config(config: Any) -> Any:
    """Wrap a config dictionary or AutoConfig for transparent attribute access."""
    if isinstance(config, ConfigWrapper):
        return config
    return ConfigWrapper(config)


def get_text_config(model_config: Any) -> Any:
    """Return inner text config for LLMs, handling nested structures."""
    if model_config is None:
        return None
    wrapped = wrap_config(model_config)
    if "text_config" in wrapped and wrapped.get("text_config") is not None:
        inner = wrapped.get("text_config")
        return wrap_config(inner)
    return wrapped


def is_quantized(model_config: Any) -> bool:
    """Return True if model configuration indicates quantization."""
    if model_config is None:
        return False
    wrapped = wrap_config(model_config)
    return "quantization_config" in wrapped and wrapped.get("quantization_config") is not None


def get_quantization_config(model_config: Any) -> dict[str, Any] | None:
    """Return quantization config dict if present."""
    if not is_quantized(model_config):
        return None
    raw = wrap_config(model_config).get("quantization_config")
    if isinstance(raw, ConfigWrapper):
        return cast(dict[str, Any], raw.raw)
    return cast(dict[str, Any], raw)


def get_quant_method(model_config: Any) -> str:
    """Determine quantization method string from quantization_config."""
    quant_cfg = get_quantization_config(model_config)
    if quant_cfg and isinstance(quant_cfg, dict):
        method = quant_cfg.get("quant_method")
        if method:
            return str(method)
    return ""


def use_mla(model_architecture: str) -> bool:
    """Return True for model architectures employing Multi-Head Latent Attention (MLA)."""
    deepseek_mla_models = (
        "DeepseekV3ForCausalLM",
        "DeepseekV2ForCausalLM",
    )
    return any(name in model_architecture for name in deepseek_mla_models)


def is_moe(model_config: Any) -> bool:
    """Return True if model architecture is Mixture of Experts."""
    if model_config is None:
        return False
    wrapped = wrap_config(model_config)
    text_cfg = get_text_config(wrapped)
    indicators = (
        "n_routed_experts",
        "n_shared_experts",
        "num_experts",
        "num_experts_per_tok",
    )
    return any(ind in text_cfg for ind in indicators)


def is_multimodal(model_config: Any) -> bool:
    """Return True if model uses a multimodal architecture."""
    if model_config is None:
        return False
    wrapped = wrap_config(model_config)
    archs = wrapped.get("architectures", [])
    if archs and isinstance(archs, (list, tuple)):
        return any(arch in MULTIMODAL_ARCHITECTURES for arch in archs)
    return False


def _extract_dtype_from_config(model_config: Any) -> str | None:
    """Extract torch_dtype or dtype attribute from config."""
    if model_config is None:
        return None
    wrapped = wrap_config(model_config)
    for attr in ("torch_dtype", "dtype"):
        val = wrapped.get(attr)
        if val is not None:
            return str(val)
    return None


def inference_dtype(model_config: Any) -> str:
    """Return KV cache inference data type string."""
    dtype = _extract_dtype_from_config(model_config)
    if dtype:
        return dtype
    if is_quantized(model_config):
        return get_quant_method(model_config)
    return ""


def precision_to_byte(precision: str) -> float:
    """Convert precision string to byte width."""
    precision = precision.strip().lower()
    mapping: dict[str, float] = {
        "f64": 8.0,
        "float64": 8.0,
        "f32": 4.0,
        "float32": 4.0,
        "f16": 2.0,
        "float16": 2.0,
        "bf16": 2.0,
        "bfloat16": 2.0,
        "f8_e5m2": 1.0,
        "f8_e4m3": 1.0,
        "fp8": 1.0,
        "fp8_e4m3": 1.0,
        "fp8_e5m2": 1.0,
        "fp4": 0.5,
        "i64": 8.0,
        "int64": 8.0,
        "i32": 4.0,
        "int32": 4.0,
        "i16": 2.0,
        "int16": 2.0,
        "i8": 1.0,
        "int8": 1.0,
        "u8": 1.0,
        "uint8": 1.0,
        "u4": 0.5,
        "i4": 0.5,
        "int4": 0.5,
        "bool": 1.0,
        "mxfp4": 4.25 / 8.0,
    }
    if precision in mapping:
        return mapping[precision]
    match = re.search(r"\d+", precision)
    if match:
        bits = int(match.group(0))
        if bits > 0:
            return bits / 8.0
    raise ValueError(f"Unsupported precision type: {precision}")


def get_quant_bytes(model_config: Any) -> float:
    """Return bytes per weight parameter based on quantization configuration."""
    quant_cfg = get_quantization_config(model_config)
    quant_method = get_quant_method(model_config)
    if quant_method:
        try:
            return precision_to_byte(quant_method)
        except ValueError:
            pass
    if quant_cfg and isinstance(quant_cfg, dict):
        if "bits" in quant_cfg:
            return float(quant_cfg["bits"]) / 8.0
        if "config_groups" in quant_cfg:
            groups = quant_cfg.get("config_groups", {})
            if isinstance(groups, dict) and "group_0" in groups:
                weights = groups["group_0"].get("weights", {})
                if "num_bits" in weights:
                    return float(weights["num_bits"]) / 8.0
    return 0.0


def inference_dtype_byte(model_config: Any) -> float:
    """Return precision for inference KV cache data type in bytes."""
    native_kv = inference_dtype(model_config)
    if native_kv:
        try:
            return precision_to_byte(native_kv)
        except ValueError:
            pass
    if is_quantized(model_config):
        q_bytes = get_quant_bytes(model_config)
        if q_bytes > 0:
            return q_bytes
    return float(DEFAULT_KV_CACHE_DTYPE_BYTES)


# ---------------------- KV Cache Computation ----------------------

@dataclass
class KVCacheDetail:
    """Structured memory calculations for KV Cache."""

    model: str
    attention_type: AttentionType
    kv_data_type: str
    precision_in_bytes: float
    num_hidden_layers: int
    hidden_size: int
    num_attention_heads: int
    num_key_value_heads: int
    head_dimension: int
    model_architecture: str

    num_attention_group: int = 1
    per_token_memory_bytes: int = 0
    per_request_kv_cache_bytes: int = 0
    per_request_kv_cache_gb: float = 0.0
    kv_cache_size_gb: float = 0.0

    context_len: int = 1
    batch_size: int = 1

    kv_lora_rank: int | None = None
    qk_rope_head_dim: int | None = None

    def __init__(
        self,
        model_name: str,
        model_config: Any,
        context_len: int = 1,
        batch_size: int = 1,
    ) -> None:
        self.model = model_name
        self.kv_data_type = inference_dtype(model_config)
        self.precision_in_bytes = inference_dtype_byte(model_config)
        wrapped = wrap_config(model_config)
        archs = wrapped.get("architectures", [])
        self.model_architecture = archs[0] if archs and isinstance(archs, (list, tuple)) else ""

        text_config = get_text_config(wrapped)
        self.num_hidden_layers = int(text_config.get("num_hidden_layers", 32))
        self.hidden_size = int(text_config.get("hidden_size", 4096))
        self.num_attention_heads = int(text_config.get("num_attention_heads", 32))
        self.num_key_value_heads = int(
            text_config.get("num_key_value_heads", self.num_attention_heads)
        )
        head_dim = text_config.get("head_dim")
        self.head_dimension = (
            int(head_dim)
            if head_dim is not None
            else int(self.hidden_size / max(1, self.num_attention_heads))
        )

        if use_mla(self.model_architecture):
            self.attention_type = AttentionType.MLA
            self.kv_lora_rank = text_config.get("kv_lora_rank", 512)
            self.qk_rope_head_dim = text_config.get("qk_rope_head_dim", 64)
        else:
            if self.num_key_value_heads == 1:
                self.attention_type = AttentionType.MQA
            elif self.num_key_value_heads == self.num_attention_heads:
                self.attention_type = AttentionType.MHA
            else:
                self.attention_type = AttentionType.GQA

        self.set_context_len(context_len)
        self.set_batch_size(batch_size)

    def set_context_len(self, context_len: int) -> None:
        self.context_len = max(1, context_len)
        self._recalculate()

    def set_batch_size(self, batch_size: int) -> None:
        self.batch_size = max(1, batch_size)
        self._recalculate()

    def _recalculate(self) -> None:
        if self.attention_type == AttentionType.MLA:
            kv_lora_rank = self.kv_lora_rank if self.kv_lora_rank is not None else 512
            qk_rope = self.qk_rope_head_dim if self.qk_rope_head_dim is not None else 64
            self.per_token_memory_bytes = int(
                self.num_hidden_layers * (kv_lora_rank + qk_rope) * self.precision_in_bytes
            )
        else:
            self.num_attention_group = max(
                1, int(self.num_attention_heads / max(1, self.num_key_value_heads))
            )
            # Factor of 2 for separate K and V caches
            self.per_token_memory_bytes = int(
                self.num_hidden_layers
                * 2
                * self.head_dimension
                * self.num_key_value_heads
                * self.precision_in_bytes
            )

        self.per_request_kv_cache_bytes = self.per_token_memory_bytes * self.context_len
        self.per_request_kv_cache_gb = bytes_to_gib(self.per_request_kv_cache_bytes)
        self.kv_cache_size_gb = self.per_request_kv_cache_gb * self.batch_size


# ---------------------- Config & Metadata Loading ----------------------

def load_local_model_config(path: str | Path) -> ConfigWrapper | None:
    """Load config from a local config.json file or folder."""
    p = Path(path)
    if p.is_dir():
        p = p / "config.json"
    if p.is_file():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
                return wrap_config(data)
        except Exception as e:
            logger.warning("Failed to load local config from %s: %s", p, e)
    return None


@lru_cache(maxsize=128)
def get_model_config_from_hf(model_name: str, hf_token: str | None = None) -> Any:
    """Load config using transformers if available, falling back to local files or None."""
    if _TRANSFORMERS_AVAILABLE and AutoConfig is not None:
        return AutoConfig.from_pretrained(
            model_name,
            trust_remote_code=True,
            token=hf_token or None,
        )
    raise ImportError(
        "Model configuration loading requires 'transformers'. "
        "Provide a local config or install transformers."
    )


def load_model_config(
    model_name_or_path: str,
    hf_token: str | None = None,
    local_config_path: str | Path | None = None,
) -> Any:
    """Load model config from local path, HuggingFace cache, or remote."""
    if local_config_path:
        cfg = load_local_model_config(local_config_path)
        if cfg is not None:
            return cfg

    # Check if model_name_or_path is itself a local path
    if os.path.exists(model_name_or_path):
        cfg = load_local_model_config(model_name_or_path)
        if cfg is not None:
            return cfg

    # Check standard HF hub cache path
    hf_home = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    hub_cache_model_dir = Path(hf_home) / "hub" / f"models--{model_name_or_path.replace('/', '--')}"
    if hub_cache_model_dir.exists():
        snapshots = hub_cache_model_dir / "snapshots"
        if snapshots.exists():
            for snap in snapshots.iterdir():
                if snap.is_dir() and (snap / "config.json").exists():
                    cfg = load_local_model_config(snap / "config.json")
                    if cfg is not None:
                        return cfg

    # Fall back to transformers remote if available
    if _TRANSFORMERS_AVAILABLE:
        try:
            return get_model_config_from_hf(model_name_or_path, hf_token)
        except Exception as err:
            logger.debug("transformers.AutoConfig failed for %s: %s", model_name_or_path, err)

    return None


@lru_cache(maxsize=128)
def _get_safetensors_metadata_cached(
    model_name: str, hf_token: str | None = None
) -> Any:
    if not _HF_AVAILABLE or HfApi is None:
        raise ImportError("HuggingFace Hub integration requires huggingface_hub.")
    api = HfApi(token=hf_token)
    return api.get_safetensors_metadata(model_name)


def model_params_by_dtype(model_name: str, hf_token: str | None = None) -> dict[str, int]:
    """Return parameter counts broken down by dtype from safetensors metadata."""
    metadata = _get_safetensors_metadata_cached(model_name, hf_token)
    return cast(dict[str, int], metadata.parameter_count)


def model_total_params(
    model_name: str,
    model_config: Any | None = None,
    hf_token: str | None = None,
    fallback_weight_gib: float | None = None,
) -> int:
    """Return the total number of parameters across all dtypes."""
    # 1. Try safetensors metadata if HF is available
    if _HF_AVAILABLE:
        try:
            metadata = _get_safetensors_metadata_cached(model_name, hf_token)
            if hasattr(metadata, "parameter_count") and metadata.parameter_count:
                return sum(metadata.parameter_count.values())
        except Exception:
            pass

    # 2. Try estimating from model_config
    if model_config is not None:
        text_cfg = get_text_config(model_config)
        hidden_size = text_cfg.get("hidden_size")
        layers = text_cfg.get("num_hidden_layers")
        intermediate = text_cfg.get("intermediate_size")
        vocab = text_cfg.get("vocab_size", 32000)
        if hidden_size and layers:
            inter = intermediate or (hidden_size * 4)
            # Rough transformer param formula: layers * (4 * hidden^2 + 3 * hidden * inter) + vocab * hidden
            per_layer = 4 * (hidden_size**2) + 3 * hidden_size * inter
            total = layers * per_layer + vocab * hidden_size
            return int(total)

    # 3. Fallback from known model weight in GiB assuming 2 bytes per param (FP16/BF16)
    if fallback_weight_gib is not None and fallback_weight_gib > 0:
        return int((fallback_weight_gib * BYTES_PER_GIB) / FP16_BF16_BYTES)

    # 4. Infer from model name heuristic (e.g. 8B -> 8_000_000_000)
    match = re.search(r"(\d+(?:\.\d+)?)[bB]\b", model_name)
    if match:
        return int(float(match.group(1)) * 1_000_000_000)

    return 7_000_000_000  # Default 7B fallback


def max_context_len(model_config: Any) -> int:
    """Return maximum context length supported by model."""
    text_cfg = get_text_config(model_config)
    if text_cfg is not None:
        for attr in ("max_position_embeddings", "model_max_length", "seq_length", "max_sequence_length"):
            val = text_cfg.get(attr)
            if val is not None and int(val) > 0:
                return int(val)
    return 4096


# ---------------------- Memory Estimations ----------------------

def estimate_vllm_non_torch_memory(tp: int = 1, pp: int = 1) -> float:
    """Estimate non-torch runtime & NCCL communication memory in GiB per GPU."""
    if tp >= 2:
        return VLLM_NON_TORCH_MEMORY_TPN_GIB
    if pp >= 2:
        return VLLM_NON_TORCH_MEMORY_TP1_PPN_GIB
    return VLLM_NON_TORCH_MEMORY_TP1_PP1_GIB


def estimate_vllm_cuda_graph_memory() -> float:
    """CUDA graph memory overhead per GPU in GiB (already accounted for in activation profile)."""
    return 0.0


def estimate_vllm_activation_memory(config: Any, tp: int = 1) -> float:
    """Estimate peak forward activation memory for vLLM inference in GiB."""
    if tp <= 0:
        raise ValueError(f"Tensor parallelism must be positive, got tp={tp}")

    if config is not None:
        wrapped = wrap_config(config)
        archs = wrapped.get("architectures", [])
        if archs and isinstance(archs, (list, tuple)):
            arch = archs[0]
            if arch in ACTIVATION_PROFILES:
                return ACTIVATION_PROFILES[arch]

        text_config = get_text_config(wrapped)
        if is_moe(text_config):
            return ACTIVATION_MEMORY_BASE_MOE_GIB
        if is_multimodal(wrapped):
            return ACTIVATION_MEMORY_BASE_MULTIMODAL_GIB

    return ACTIVATION_MEMORY_BASE_DENSE_GIB


def parameter_memory_req(parameter: int, precision: str) -> float:
    """Calculate memory requirement (in GiB) for parameter count with given precision string."""
    precision_byte = precision_to_byte(precision)
    return bytes_to_gib(parameter * precision_byte)


def parameter_precision_memory_req(parameter: int, precision_in_byte: float) -> float:
    """Calculate memory requirement (in GiB) for parameter count with given precision in bytes."""
    return bytes_to_gib(parameter * precision_in_byte)


def model_memory_req(
    model_name: str,
    model_config: Any | None = None,
    hf_token: str | None = None,
    fallback_weight_gib: float | None = None,
) -> float:
    """Calculate the GPU memory (in GiB) required for loading model weights."""
    # If safetensors metadata is available via HF
    if _HF_AVAILABLE:
        try:
            model_params = model_params_by_dtype(model_name, hf_token)
            if model_params:
                quant_method = get_quant_method(model_config) if model_config and is_quantized(model_config) else ""
                if quant_method == "mxfp4":
                    return sum(parameter_memory_req(num, prec) for prec, num in model_params.items())

                quant_byte = get_quant_bytes(model_config) if quant_method else None
                memory: float = 0.0
                for prec, num in model_params.items():
                    prec_byte = precision_to_byte(prec)
                    if prec_byte >= 2:
                        memory += parameter_memory_req(num, prec)
                    elif quant_byte is not None:
                        memory += parameter_precision_memory_req(num, quant_byte)
                    else:
                        memory += parameter_memory_req(num, prec)
                return memory
        except Exception:
            pass

    # If caller gave an explicit weight in GiB, use it directly
    if fallback_weight_gib is not None and fallback_weight_gib > 0:
        return float(fallback_weight_gib)

    # Derive from parameter count and precision
    total_params = model_total_params(model_name, model_config, hf_token)
    dtype_byte = inference_dtype_byte(model_config) if model_config else FP16_BF16_BYTES
    return parameter_precision_memory_req(total_params, dtype_byte)


def kv_cache_req(
    model_name: str,
    model_config: Any,
    context_len: int,
    batch_size: int = 1,
) -> float:
    """Calculate KV cache requirement in GiB."""
    return KVCacheDetail(model_name, model_config, context_len, batch_size).kv_cache_size_gb


# ---------------------- Parallelism & Capacity Sizing ----------------------

def _pad_vocab_size(vocab_size: int, pad_to: int = _VLLM_VOCAB_PADDING) -> int:
    """Match vLLM's vocab padding logic (rounds up to nearest multiple of pad_to)."""
    return ((vocab_size + pad_to - 1) // pad_to) * pad_to


def find_possible_tp(model_config: Any) -> list[int]:
    """Find valid tensor parallelism (TP) values satisfying vLLM sharding constraints."""
    text_config = get_text_config(model_config)
    if text_config is None:
        return [1]

    num_attention_heads = text_config.get("num_attention_heads")
    if num_attention_heads is None:
        return [1]
    num_attention_heads = int(num_attention_heads)
    num_kv_heads = int(text_config.get("num_key_value_heads", num_attention_heads))
    intermediate_size = text_config.get("intermediate_size")
    vocab_size = text_config.get("vocab_size")

    padded_vocab_size = _pad_vocab_size(int(vocab_size)) if vocab_size is not None else None

    valid: list[int] = []
    for i in range(1, num_attention_heads + 1):
        if num_attention_heads % i != 0:
            continue
        if not (num_kv_heads % i == 0 or i % num_kv_heads == 0):
            continue
        if intermediate_size is not None and int(intermediate_size) % i != 0:
            continue
        if padded_vocab_size is not None and padded_vocab_size % i != 0:
            continue
        valid.append(i)

    return sorted(valid) if valid else [1]


def available_gpu_memory(memory: float, gpu_utilization: float = 0.9) -> float:
    """Return available memory in GiB for a given GPU capacity and utilization ratio."""
    return float(memory) * float(gpu_utilization)


def gpus_required(tp: int = 1, pp: int = 1, dp: int = 1) -> int:
    """Return total GPUs required across parallelism dimensions."""
    return max(1, tp) * max(1, pp) * max(1, dp)


def per_gpu_model_memory_required(
    model_name: str,
    model_config: Any,
    tp: int = 1,
    pp: int = 1,
    hf_token: str | None = None,
    fallback_weight_gib: float | None = None,
) -> float:
    """Calculate model weight memory required per GPU in GiB."""
    model_memory = model_memory_req(
        model_name, model_config, hf_token=hf_token, fallback_weight_gib=fallback_weight_gib
    )
    return model_memory / (max(1, tp) * max(1, pp))


def allocatable_kv_cache_memory(
    model_name: str,
    model_config: Any,
    gpu_memory: float,
    gpu_util: float = 0.9,
    tp: int = 1,
    pp: int = 1,
    dp: int = 1,
    max_model_len: int | None = None,
    batch_size: int = 1,
    hf_token: str | None = None,
    fallback_weight_gib: float | None = None,
    gpu_mem_util: float | None = None,
) -> float:
    """Calculate allocatable memory for KV cache in GiB across replicas.

    Formula:
    Available = (GPU_memory * utilization * num_GPUs)
              - (Model_weights * DP)
              - (Activation_memory * DP)
              - Non_torch_overhead
    """
    effective_util = gpu_mem_util if gpu_mem_util is not None else gpu_util
    gpu_count = gpus_required(tp, pp, dp)
    available_memory = available_gpu_memory(gpu_memory, effective_util) * gpu_count
    model_size = model_memory_req(
        model_name, model_config, hf_token=hf_token, fallback_weight_gib=fallback_weight_gib
    ) * dp

    activation_memory = estimate_vllm_activation_memory(model_config, tp=tp) * dp
    cuda_graph_memory = estimate_vllm_cuda_graph_memory() * gpu_count
    non_torch_memory = estimate_vllm_non_torch_memory(tp, pp) * gpu_count

    total_consumed = model_size + activation_memory + cuda_graph_memory + non_torch_memory
    # Return raw difference; callers inspect whether < 0 for OOM detection
    return available_memory - total_consumed


def max_concurrent_requests(
    model_name: str,
    model_config: Any,
    max_model_len: int,
    gpu_memory: float,
    gpu_util: float = 0.9,
    batch_size: int = 1,
    tp: int = 1,
    pp: int = 1,
    dp: int = 1,
    hf_token: str | None = None,
    fallback_weight_gib: float | None = None,
    gpu_mem_util: float | None = None,
) -> int:
    """Calculate maximum number of concurrent requests that can fit in KV cache."""
    effective_util = gpu_mem_util if gpu_mem_util is not None else gpu_util
    kv_cache_allocatable = allocatable_kv_cache_memory(
        model_name,
        model_config,
        gpu_memory,
        effective_util,
        tp,
        pp,
        dp,
        max_model_len=max_model_len,
        batch_size=batch_size,
        hf_token=hf_token,
        fallback_weight_gib=fallback_weight_gib,
    )

    per_request_kv = kv_cache_req(model_name, model_config, max_model_len)
    if per_request_kv <= 0 or kv_cache_allocatable <= 0:
        return 0
    return max(0, math.floor(kv_cache_allocatable / per_request_kv))


def check_model_fits_gpu(
    model_name: str,
    model_config: Any,
    gpu_memory_gb: float,
    gpu_util: float = 0.9,
    hf_token: str | None = None,
    fallback_weight_gib: float | None = None,
) -> list[int]:
    """Return sorted list of valid TP values where model leaves positive KV cache memory."""
    valid_tps: list[int] = []
    for tp in find_possible_tp(model_config):
        available = allocatable_kv_cache_memory(
            model_name,
            model_config,
            gpu_memory_gb,
            gpu_util,
            tp=tp,
            hf_token=hf_token,
            fallback_weight_gib=fallback_weight_gib,
        )
        if available > 0:
            valid_tps.append(tp)
    return valid_tps


def auto_max_model_len(
    model_name: str,
    model_config: Any,
    gpu_memory: float,
    gpu_mem_util: float = 0.9,
    tp: int = 1,
    pp: int = 1,
    dp: int = 1,
    hf_token: str | None = None,
    fallback_weight_gib: float | None = None,
    gpu_util: float | None = None,
) -> int:
    """Calculate the maximum context length that fits in available GPU memory for 1 request."""
    effective_util = gpu_util if gpu_util is not None else gpu_mem_util
    allocatable_kv = allocatable_kv_cache_memory(
        model_name,
        model_config,
        gpu_memory,
        effective_util,
        tp,
        pp,
        dp,
        max_model_len=1,
        batch_size=1,
        hf_token=hf_token,
        fallback_weight_gib=fallback_weight_gib,
    )

    if allocatable_kv <= 0:
        return 0

    kv_detail = KVCacheDetail(model_name, model_config, context_len=1, batch_size=1)
    per_token_bytes = kv_detail.per_token_memory_bytes / (tp * pp)
    if per_token_bytes <= 0:
        return 0

    max_tokens = int(gib_to_bytes(allocatable_kv) // per_token_bytes)
    if max_tokens <= 0:
        return 0

    try:
        model_max = max_context_len(model_config)
    except Exception:
        model_max = max_tokens

    return min(max_tokens, model_max)


def total_kv_cache_blocks(
    model_name: str,
    model_config: Any,
    context_len: int,
    gpu_memory: float,
    gpu_mem_util: float = 0.9,
    batch_size: int = 1,
    block_size: int = 16,
    tp: int = 1,
    pp: int = 1,
    dp: int = 1,
    hf_token: str | None = None,
    fallback_weight_gib: float | None = None,
    gpu_util: float | None = None,
) -> int:
    """Calculate total number of KV cache blocks that fit in GPU memory."""
    effective_util = gpu_util if gpu_util is not None else gpu_mem_util
    kv_cache_detail = KVCacheDetail(model_name, model_config, context_len, batch_size)
    per_token_memory = kv_cache_detail.per_token_memory_bytes / (tp * pp)
    per_block_memory = per_token_memory * block_size

    kv_cache_allocatable = allocatable_kv_cache_memory(
        model_name,
        model_config,
        gpu_memory,
        effective_util,
        tp,
        pp,
        dp,
        max_model_len=context_len,
        batch_size=batch_size,
        hf_token=hf_token,
        fallback_weight_gib=fallback_weight_gib,
    )
    if kv_cache_allocatable <= 0 or per_block_memory <= 0:
        return 0
    return int(gib_to_bytes(kv_cache_allocatable) // per_block_memory)


# ---------------------- Basic Math Conversion ----------------------

def bits_to_bytes(bits: int | float) -> int:
    """Convert number of bits to bytes."""
    return int(bits / 8)


def bytes_to_gib(num_bytes: float) -> float:
    """Convert bytes to gibibytes (GiB)."""
    return num_bytes / BYTES_PER_GIB


def gib_to_bytes(gib: float) -> float:
    """Convert gibibytes (GiB) to bytes."""
    return gib * BYTES_PER_GIB
