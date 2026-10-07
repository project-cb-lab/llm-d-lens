"""Built-in vLLM memory overhead constants and architectural calibration profiles.

All memory values are in GiB, calibrated against vLLM v0.19.0 on H100-80GB
(derived from empirical profiling runs across diverse open-weights models).
Embedded directly as pure Python constants with zero runtime I/O overhead.
"""

from __future__ import annotations

# Computational & precision constants
BYTES_PER_GIB = 1024**3
FP16_BF16_BYTES = 2
HIGH_PRECISION_THRESHOLD_BYTES = 2
DEFAULT_KV_CACHE_DTYPE_BYTES = 1

# Base activation memory by model architecture category (in GiB)
ACTIVATION_BASE_GIB: dict[str, float] = {
    "dense": 2.00,
    "moe": 2.50,
    "multimodal": 2.00,
}

ACTIVATION_MEMORY_BASE_DENSE_GIB: float = ACTIVATION_BASE_GIB["dense"]
ACTIVATION_MEMORY_BASE_MOE_GIB: float = ACTIVATION_BASE_GIB["moe"]
ACTIVATION_MEMORY_BASE_MULTIMODAL_GIB: float = ACTIVATION_BASE_GIB["multimodal"]

# Non-torch runtime and communication overhead (in GiB)
NON_TORCH_OVERHEAD_GIB: dict[str, float] = {
    "tp1_pp1": 0.27,
    "tp1_ppN": 0.07,
    "tpN_pp1": 2.10,
    "tpN_ppN": 2.10,
}

VLLM_NON_TORCH_MEMORY_TP1_PP1_GIB: float = NON_TORCH_OVERHEAD_GIB["tp1_pp1"]
VLLM_NON_TORCH_MEMORY_TP1_PPN_GIB: float = NON_TORCH_OVERHEAD_GIB["tp1_ppN"]
VLLM_NON_TORCH_MEMORY_TPN_GIB: float = NON_TORCH_OVERHEAD_GIB["tpN_pp1"]
VLLM_NON_TORCH_MEMORY_TP1_GIB: float = VLLM_NON_TORCH_MEMORY_TP1_PP1_GIB

# Per-architecture activation profiles loaded from calibration results (in GiB)
ACTIVATION_PROFILES: dict[str, float] = {
    "LlamaForCausalLM": 1.89,
    "Qwen2ForCausalLM": 2.25,
    "Qwen3ForCausalLM": 2.21,
    "Mistral3ForConditionalGeneration": 2.10,
    "PixtralForConditionalGeneration": 2.10,
    "DeepseekV2ForCausalLM": 1.93,
    "DeepseekV3ForCausalLM": 1.93,
    "GraniteForCausalLM": 0.85,
    "LlavaNextForConditionalGeneration": 0.79,
    "MixtralForCausalLM": 1.21,
    "Phi3ForCausalLM": 1.52,
    "Qwen2MoeForCausalLM": 2.47,
    "Qwen3MoeForCausalLM": 2.68,
    "Gemma2ForCausalLM": 3.65,
    "Gemma3ForCausalLM": 3.94,
    "GemmaForCausalLM": 3.63,
}

# Multimodal architectures where vision encoder does not participate in CUDA graphs
MULTIMODAL_ARCHITECTURES: tuple[str, ...] = (
    "PixtralForConditionalGeneration",
    "Mistral3ForConditionalGeneration",
    "LlavaForConditionalGeneration",
    "LlavaNextForConditionalGeneration",
)
