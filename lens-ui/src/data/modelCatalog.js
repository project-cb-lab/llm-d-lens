// Shared static model catalog data, used by the Model Market page and
// (read-only, for type lookup) the Model Cache page. This is a frontend-only
// catalog -- there is no backend "model market" API, so both pages import
// this same array rather than duplicating it.
import metadata from './modelCatalogMetadata.json' with { type: 'json' };

const rows = `2026-08-09|meta-models/Muse-Glimmer-30B|BF16|image-text-to-text
2026-06-05|google/gemma-4-12B-it-qat-w4a16-ct|INT4|any-to-any
2026-04-22|deepseek-ai/DeepSeek-V4-Flash|BF16|text-generation
2025-01-01|Qwen/Qwen3-0.6B|BF16|text-generation
2025-04-29|Qwen/Qwen3-8B|BF16|text-generation
2026-03-24|CohereLabs/cohere-transcribe-03-2026|BF16|automatic-speech-recognition
2026-03-18|baidu/Qianfan-OCR|BF16|image-text-to-text
2026-03-11|google/gemma-4-31B-it|BF16|image-text-to-text
2026-03-11|google/gemma-4-26B-A4B-it|BF16|image-text-to-text
2026-03-10|RedHatAI/Ministral-3-3B-Instruct-2512|FP8|image-text-to-text
2026-03-03|sarvamai/sarvam-30b|FP32|text-generation
2026-02-24|Qwen/Qwen3.5-35B-A3B|BF16|image-text-to-text
2026-02-12|ATH-MaaS/Ovis2.6-30B-A3B|BF16|image-text-to-text
2026-01-30|zai-org/GLM-OCR|BF16|image-text-to-text
2026-01-28|Qwen/Qwen3-ASR-1.7B|BF16|automatic-speech-recognition
2026-01-27|deepseek-ai/DeepSeek-OCR-2|BF16|image-text-to-text
2026-01-19|zai-org/GLM-4.7-Flash|BF16|text-generation
2026-01-19|RedHatAI/granite-4.0-h-tiny-FP8-dynamic|FP8|image-text-to-text
2026-01-13|stepfun-ai/Step3-VL-10B|BF16|image-text-to-text
2025-12-30|IQuestLab/IQuest-Coder-V1-40B-Loop-Instruct|BF16|text-generation
2025-12-30|IQuestLab/IQuest-Coder-V1-40B-Instruct|BF16|text-generation
2025-12-14|allenai/Molmo2-4B|FP32|image-text-to-text
2025-12-10|PerceptronAI/Isaac-0.2B-Preview|FP32|image-text-to-text
2025-12-09|inceptionai/Jais-2-8B-Chat|BF16|image-text-to-text
2025-12-01|arcee-ai/Trinity-Nano-Base|BF16|text-generation
2025-11-19|allenai/Olmo-3-7B-Instruct|BF16|image-text-to-text
2025-11-18|tencent/HunyuanOCR|BF16|image-text-to-text
2025-10-31|mistralai/Ministral-3-14B-Instruct-2512|FP8|image-text-to-text
2025-10-30|FreedomIntelligence/openPangu-Embedded-7B-V1.1|BF16|text-generation
2025-10-24|pfnet/plamo-3-nict-2b-base|BF16|text-generation
2025-10-24|nvidia/audio-flamingo-3-hf|BF16|audio-text-to-text
2025-10-20|lightonai/LightOnOCR-1B-1025|BF16|image-to-text
2025-10-16|PaddlePaddle/PaddleOCR-VL|BF16|image-text-to-text
2025-10-16|ibm-granite/granite-3.3-8b-instruct-FP8|FP8|image-text-to-text
2025-09-18|RedHatAI/Apertus-8B-Instruct-2509-FP8-dynamic|FP8|text-generation
2025-09-09|Qwen/Qwen3-Next-80B-A3B-Thinking|BF16|text-generation
2025-09-09|Qwen/Qwen3-Next-80B-A3B-Instruct|BF16|text-generation
2025-09-02|inclusionAI/Ling-mini-2.0|BF16|text-generation
2025-08-26|Kwai-Keye/Keye-VL-1_5-8B|BF16|video-text-to-text
2025-08-25|OpenGVLab/InternVL3_5-8B|BF16|image-text-to-text
2025-08-25|OpenGVLab/InternVL3_5-38B|BF16|image-text-to-text
2025-08-25|OpenGVLab/InternVL3_5-30B-A3B|BF16|image-text-to-text
2025-08-25|OpenGVLab/InternVL3_5-14B|BF16|image-text-to-text
2025-08-25|facebook/cwm|BF16|text-generation
2025-08-18|internlm/Intern-S1-mini|BF16|image-text-to-text
2025-08-15|ATH-MaaS/Ovis2.5-9B|BF16|image-text-to-text
2025-08-15|ATH-MaaS/Ovis2.5-2B|BF16|image-text-to-text
2025-08-12|LiquidAI/LFM2-VL-450M|BF16|image-text-to-text
2025-08-11|YannQi/R-4B|FP32|image-text-to-text
2025-08-04|openai/gpt-oss-20b|MXFP4|text-generation
2025-08-04|openai/gpt-oss-120b|MXFP4|text-generation
2025-07-31|Qwen/Qwen3-coder-30B-A3B-Instruct|BF16|text-generation
2025-07-30|tencent/Hunyuan-7B-Instruct|BF16|text-generation
2025-07-29|arcee-ai/AFM-4.5B-Base|FP32|text-generation
2025-07-23|kakaocorp/kanana-1.5-v-3b-instruct|BF16|image-text-to-text
2025-07-17|ibm-granite/granite-embedding-english-r2|BF16|feature-extraction
2025-07-12|openbmb/MiniCPM-V-4|BF16|image-text-to-text
2025-07-11|LGAI-EXAONE/EXAONE-4.0-32B|BF16|text-generation
2025-07-10|LiquidAI/LFM2-350M|BF16|text-generation
2025-06-28|baidu/ERNIE-4.5-VL-28B-A3B-PT|BF16|image-text-to-text
2025-06-28|baidu/ERNIE-4.5-21B-A3B-PT|BF16|text-generation
2025-06-28|baidu/ERNIE-4.5-0.3B-PT|BF16|text-generation
2025-06-26|mispeech/midashenglm-7b|FP32|audio-text-to-text
2025-06-26|Kwai-Keye/Keye-VL-8B-Preview|BF16|video-text-to-text
2025-06-25|tencent/Hunyuan-A13B-Instruct|BF16|text-generation
2025-06-12|google/gemma-3n-E2B-it|BF16|image-text-to-text
2025-06-11|allenai/FlexOlmo-7x7B-1T|FP32|text-generation
2025-06-03|Qwen/Qwen3-Embedding-8B|BF16|feature-extraction
2025-06-03|nvidia/Llama-3.1-Nemotron-Nano-VL-8B-V1|BF16|image-text-to-text
2025-05-29|Qwen/Qwen3-Reranker-8B|BF16|text-ranking
2025-05-19|ByteDance-Seed/BAGEL-7B-MoT|BF16|any-to-any
2025-05-11|inclusionAI/Ling-lite-1.5|BF16|text-generation
2025-05-05|Qwen/Qwen3-30B-A3B-GPTQ-Int4|Int4|text-generation
2025-04-29|XiaomiMiMo/MiMo-7B-RL|BF16|text-generation
2025-04-28|Qwen/Qwen3-30B-A3B-FP8|FP8|text-generation
2025-04-27|Qwen/Qwen3-32B|BF16|text-generation
2025-04-27|Qwen/Qwen3-30B-A3B|BF16|text-generation
2025-04-27|Qwen/Qwen3-14B|BF16|text-generation
2025-04-22|naver-hyperclovax/HyperCLOVAX-SEED-Vision-Instruct-3B|BF16|image-text-to-text
2025-04-17|allenai/OLMo-2-0425-1B|FP32|text-generation
2025-04-14|ibm-granite/granite-speech-3.3-8b|BF16|automatic-speech-recognition
2025-04-12|nvidia/Eagle2.5-8B|BF16|image-text-to-text
2025-03-21|Qwen/Qwen2.5-VL-32B-Instruct|BF16|image-text-to-text
2025-03-17|Skywork/Skywork-R1V-38B|BF16|image-text-to-text
2025-03-16|nvidia/Llama-3_3-Nemotron-Super-49B-v1|BF16|text-generation
2025-03-11|mistralai/Mistral-Small-3.1-24B-Instruct-2503|BF16|image-text-to-text
2025-03-10|google/gemma-3-1b-it|BF16|text-generation
2025-03-05|Qwen/QwQ-32B|BF16|text-generation
2025-03-02|CohereLabs/aya-vision-8b|FP16|image-text-to-text
2025-03-01|google/gemma-3-27b-it|BF16|image-text-to-text
2025-02-24|microsoft/Phi-4-multimodal-instruct|BF16|automatic-speech-recognition
2025-02-17|ibm-granite/granite-3.2-2b-instruct|BF16|text-generation
2025-02-08|HuggingFaceTB/SmolVLM2-2.2B-Instruct|FP32|image-text-to-text
2025-02-06|fixie-ai/ultravox-v0_5-llama-3_2-1b|BF16|audio-text-to-text
2025-01-27|Qwen/Qwen2.5-VL-72B-Instruct|BF16|image-text-to-text
2025-01-20|deepseek-ai/DeepSeek-R1-Distill-Qwen-32B|BF16|text-generation
2025-01-20|deepseek-ai/DeepSeek-R1-Distill-Qwen-14B|BF16|text-generation
2025-01-20|deepseek-ai/DeepSeek-R1-Distill-Llama-8B|BF16|text-generation
2025-01-20|deepseek-ai/DeepSeek-R1-Distill-Llama-70B|BF16|text-generation
2025-01-13|internlm/internlm3-8b-instruct|BF16|text-generation
2025-01-12|openbmb/MiniCPM-o-2_6|BF16|any-to-any`;

const categoryFor = (modelType) => {
    if (modelType === 'feature-extraction') return 'Encoder';
    if (modelType === 'text-ranking') return 'Rerank';
    if (modelType.includes('speech') || modelType.includes('audio')) return 'Speech';
    if (modelType.includes('image') || modelType.includes('video')) return 'Vision';
    return 'Text';
};

export const MODELS = rows.split('\n').map((row) => {
    const [releaseDate, repository, dtype, modelType] = row.split('|');
    const details = metadata[repository] || {};
    return {
        id: repository.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''),
        repository,
        name: repository.split('/').at(-1),
        family: repository.split('/')[0],
        releaseDate,
        dtype,
        modelType,
        category: categoryFor(modelType),
        sizeGiB: details.weightGiB || 0,
        context: details.contextLength ? String(details.contextLength) : 'Not listed',
        weightGiB: details.weightGiB,
        contextLength: details.contextLength,
        vllmSupported: true,
    };
});

export const CATEGORIES = ['All', ...new Set(MODELS.map((model) => model.category))];

// Strip a HuggingFace revision suffix ("org/repo@revision" -> "org/repo") and
// normalize case so lookups are forgiving about how the repo id was typed.
function normalizeRepository(repository) {
    return String(repository || '').split('@')[0].trim().toLowerCase();
}

const MODELS_BY_REPOSITORY = new Map(MODELS.map((model) => [normalizeRepository(model.repository), model]));
const MODELS_BY_ID = new Map(MODELS.map((model) => [model.id, model]));

// Look up a catalog entry by HuggingFace repository id (or model-catalog id
// as a fallback). Returns undefined when the model isn't in the catalog.
export function findModelByRepository(repository) {
    if (!repository) return undefined;
    return MODELS_BY_REPOSITORY.get(normalizeRepository(repository)) || MODELS_BY_ID.get(repository);
}
