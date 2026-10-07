// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Voice input for "Chat with Lens": fully client-side speech-to-text, no
// backend changes. Runs a small (~39M param) Whisper model entirely in the
// browser via transformers.js (WebAssembly + CPU, no GPU/server round trip),
// mirroring what a "small STT model that runs on CPU" means, just moved to
// the browser's CPU instead of Prism's backend. The model is fetched lazily
// (once, on first use) straight from the Hugging Face Hub CDN and cached by
// the browser, so it costs nothing on every other page load.
//
// "onnx-community/whisper-tiny" (not the .en variant, and not the older
// "Xenova/whisper-tiny") is used deliberately:
//  - multilingual, since Prism's own operators have been chatting with Lens
//    in both English and Chinese in this session ("*.en" would only
//    understand English).
//  - the "onnx-community" org re-exports models specifically for this v3+
//    @huggingface/transformers pipeline (the older "Xenova/*" ONNX exports,
//    made for the predecessor @xenova/transformers v2, are missing some
//    metadata this library's ONNX Runtime Web build expects).
let transcriberPromise = null;

async function getTranscriber(onProgress) {
    if (!transcriberPromise) {
        transcriberPromise = import('@huggingface/transformers')
            .then(({ pipeline }) =>
                pipeline('automatic-speech-recognition', 'onnx-community/whisper-tiny', {
                    // NOT 'q8'/quantized: the merged-decoder quantized export
                    // for this model has a broken/incompatible scale tensor
                    // for its (embed_tokens-tied) MatMulNBits weight in this
                    // onnxruntime-web version, which fails session creation
                    // with "TransposeDQWeightsForMatMulNBits ... Missing
                    // required scale". fp32 sidesteps that broken graph path
                    // entirely; whisper-tiny is small enough (~39M params)
                    // that full precision is still fast in-browser.
                    dtype: 'fp32',
                    progress_callback: onProgress,
                }),
            )
            .catch((error) => {
                // Don't cache a failed load -- let the next mic click retry
                // instead of permanently failing for the rest of the session.
                transcriberPromise = null;
                throw error;
            });
    }
    return transcriberPromise;
}

// Decodes a recorded audio Blob (whatever the browser's MediaRecorder
// produced, typically audio/webm;codecs=opus) into the mono 16kHz Float32
// PCM samples transformers.js' Whisper pipeline expects, using the Web
// Audio API's own decoder/resampler instead of pulling in an extra decoding
// dependency.
async function decodeToMono16k(blob) {
    const arrayBuffer = await blob.arrayBuffer();
    const AudioContextImpl = window.AudioContext || window.webkitAudioContext;
    const audioContext = new AudioContextImpl({ sampleRate: 16000 });
    try {
        const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);
        return audioBuffer.getChannelData(0);
    } finally {
        audioContext.close();
    }
}

// Lazily built Traditional -> Simplified Chinese converter (opencc-js).
// Whisper has no separate "script" control -- `language: 'chinese'` only
// picks Mandarin, not simplified vs. traditional characters -- and since its
// training data is mostly-but-not-exclusively simplified, occasional
// traditional characters slip into the output. Converting from 't'
// (traditional, superset) to 'cn' (simplified) is idempotent on text that's
// already simplified, so it's safe to always apply.
let zhConverterPromise = null;
function getZhConverter() {
    if (!zhConverterPromise) {
        zhConverterPromise = import('opencc-js').then(({ Converter }) => Converter({ from: 't', to: 'cn' }));
    }
    return zhConverterPromise;
}

// Transcribes one recorded utterance. `onModelProgress` (optional) receives
// transformers.js' download-progress events the first time it's called, so
// the caller can show "Downloading speech model..." only on that first use.
// `language` (optional, e.g. 'english'/'chinese') is forwarded straight to
// Whisper's decoder as a forced language hint: whisper-tiny's built-in
// language auto-detection is unreliable on short clips, so letting the user
// pick the language up front avoids it guessing wrong and transcribing
// gibberish in the wrong language.
export async function transcribeAudioBlob(blob, { onModelProgress, language } = {}) {
    const [transcriber, audioData] = await Promise.all([getTranscriber(onModelProgress), decodeToMono16k(blob)]);
    const result = await transcriber(audioData, language ? { language, task: 'transcribe' } : undefined);
    const text = Array.isArray(result) ? result[0]?.text : result?.text;
    const trimmed = (text || '').trim();
    if (language === 'chinese' && trimmed) {
        const convert = await getZhConverter();
        return convert(trimmed);
    }
    return trimmed;
}

// Languages offered in the UI's voice-input language picker. Kept as a small
// fixed list (rather than Whisper's full ~100-language set) since these are
// the languages Prism operators have actually used with Chat with Lens.
export const SPEECH_TO_TEXT_LANGUAGES = [
    { value: 'english', label: 'English' },
    { value: 'chinese', label: 'Chinese' },
];

export function isSpeechToTextSupported() {
    return typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia && typeof window.MediaRecorder !== 'undefined';
}

// Browsers only expose navigator.mediaDevices.getUserMedia in a "secure
// context" (HTTPS, or http://localhost). When Prism is reached over plain
// HTTP on a non-localhost host/IP, mediaDevices is undefined and
// isSpeechToTextSupported() above returns false -- not because the browser
// lacks the feature, but because the page itself can't use it. Surfacing
// this distinction lets the UI explain *why* voice input is unavailable
// instead of silently hiding the button.
export function getMicUnavailableReason() {
    if (typeof window === 'undefined') return '';
    if (typeof window.MediaRecorder === 'undefined') {
        return 'This browser does not support audio recording (MediaRecorder API).';
    }
    if (!navigator.mediaDevices?.getUserMedia) {
        if (window.isSecureContext === false) {
            return 'Voice input requires HTTPS (or accessing Prism via http://localhost). Ask your admin to serve Prism over HTTPS, or use an SSH port-forward to localhost.';
        }
        return 'This browser does not support microphone access.';
    }
    return '';
}
