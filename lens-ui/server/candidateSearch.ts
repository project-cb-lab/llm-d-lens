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

import express from 'express';
import { fetchJsonWithTimeout } from './http';
import { aicAcceleratorForSystem, loadHardwareProfiles, type HardwareProfile } from './hardwareProfiles.ts';
import { internalHeadersFor } from './internalAuth.ts';

export const candidateSearchRouter = express.Router();

const AIC_API_URL = (process.env.AIC_API_URL || process.env.CONFIGURATION_API_URL || process.env.SIMULATION_API_URL || 'http://127.0.0.1:8081').replace(/\/$/, '');
const REQUEST_TIMEOUT_MS = Number(process.env.AIC_REQUEST_TIMEOUT_MS || 240_000);
const SEARCH_SOURCES = new Set(['baseline', 'pd', 'epd', 'tiered-cache', 'precise-prefix-cache']);
const SOURCE_LABELS: Record<string, string> = {
    aic: 'AIC Prediction',
    baseline: 'Optimized Baseline',
    pd: 'P/D Disaggregation',
    epd: 'E/P/D Disaggregation',
    'tiered-cache': 'Tiered Prefix Cache',
    'precise-prefix-cache': 'Precise Prefix Cache Routing',
    manual: 'Manual Configuration',
};

type JsonRecord = Record<string, unknown>;

function positiveInteger(value: unknown, fallback: number, maximum = 128): number {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed > 0 ? Math.min(parsed, maximum) : fallback;
}

function finiteNumber(value: unknown): number | null {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}

function acceleratorForSystem(systemName: string, profiles: HardwareProfile[] = []): 'xpu' | 'cuda' {
    // Prefer the registered profile's planning patterns; keep the historical
    // regex as a fallback when the registry is unavailable.
    return aicAcceleratorForSystem(systemName, profiles)
        || (/bmg|max_|xpu|b60|pvc/i.test(systemName) ? 'xpu' : 'cuda');
}

function predictPayload(workload: JsonRecord, searchConfig: JsonRecord, profiles: HardwareProfile[] = []): JsonRecord {
    const aicSystemName = String(searchConfig.aicSystemName || '').trim();
    return {
        model_name: String(workload.model || ''),
        gpu_count: positiveInteger(searchConfig.totalGpus, 8, 64),
        mean_input_tokens: positiveInteger(workload.isl, 1024, 1_000_000),
        mean_output_tokens: positiveInteger(workload.osl, 256, 1_000_000),
        ttft_target_ms: finiteNumber(workload.ttftMs),
        tpot_target_ms: finiteNumber(workload.tpotMs),
        aic_system_name: aicSystemName || 'b60',
        aic_backend_name: String(searchConfig.aicBackendName || 'vllm').trim().toLowerCase(),
        aic_database_mode: String(searchConfig.aicDatabaseMode || 'SILICON').trim().toUpperCase(),
        accelerator: acceleratorForSystem(aicSystemName, profiles),
    };
}

async function aicFetch(path: string, init?: RequestInit): Promise<JsonRecord> {
    try {
        const headers = new Headers(init?.headers);
        for (const [name, value] of Object.entries(internalHeadersFor(init?.method || 'GET', path))) {
            headers.set(name, value);
        }
        const { response, payload } = await fetchJsonWithTimeout(
            `${AIC_API_URL}${path}`, { ...init, headers }, REQUEST_TIMEOUT_MS,
        );
        if (!response.ok) {
            const detail = payload.detail;
            throw Object.assign(new Error(typeof detail === 'string' ? detail : `AIC returned HTTP ${response.status}`), {
                status: response.status,
                noFeasible: response.status === 400 && (detail === 'No feasible configurations found for the given parameters.'
                    || (typeof detail === 'object' && detail !== null
                        && (detail as JsonRecord).error === 'No feasible configurations found for the given parameters.')),
            });
        }
        return payload;
    } catch (error) {
        if (error instanceof Error && error.name === 'AbortError') {
            throw Object.assign(new Error('AIC candidate search timed out'), { status: 504 });
        }
        throw error;
    }
}

function candidateBase(source: string, configId: string, index: number): JsonRecord {
    return {
        id: `${source}-${configId}-${index}`,
        source,
        sourceLabel: SOURCE_LABELS[source] || source,
        family: source === 'aic' ? 'Predictive' : 'Search-based',
        confidence: null,
        backend: 'AIConfigurator',
    };
}

function normalizeAicCandidate(raw: JsonRecord, index: number): JsonRecord {
    const mode = String(raw.mode || 'disagg');
    const source = 'aic';
    const isAggregated = mode === 'agg';
    const prefillTp = isAggregated ? 0 : positiveInteger(raw.prefill_tp, 1);
    const prefillReplicas = isAggregated ? 0 : positiveInteger(raw.prefill_replicas, 1);
    const decodeTp = isAggregated ? positiveInteger(raw.tp, 1) : positiveInteger(raw.decode_tp, 1);
    const decodeReplicas = isAggregated ? positiveInteger(raw.replicas, 1) : positiveInteger(raw.decode_replicas, 1);
    const computedGpus = prefillTp * prefillReplicas + decodeTp * decodeReplicas;
    const totalGpus = positiveInteger(raw.num_total_gpus, computedGpus);
    const configId = mode === 'agg'
        ? `agg-tp${decodeTp}-rep${decodeReplicas}`
        : `${mode}-p${prefillTp}x${prefillReplicas}-d${decodeTp}x${decodeReplicas}`;
    return {
        ...candidateBase(source, configId, index),
        name: `AIC #${index + 1} · ${configId}`,
        topologyMode: mode,
        prefillTp,
        prefillReplicas,
        decodeTp,
        decodeReplicas,
        encodeTp: mode === 'epd' ? positiveInteger(raw.encode_tp, 1) : null,
        encodeReplicas: mode === 'epd' ? positiveInteger(raw.encode_replicas, 1) : null,
        totalGpus,
        predicted: {
            ttftMs: finiteNumber(raw.ttft_ms),
            tpotMs: finiteNumber(raw.tpot_ms),
            throughputTps: finiteNumber(raw.throughput_tokens_per_sec),
        },
        aicRank: index + 1,
        deployable: mode !== 'epd',
        deploymentNote: mode === 'epd' ? 'The current deployment backend does not support E/P/D topology.' : null,
    };
}

function completeAicCandidate(candidate: JsonRecord, workload: JsonRecord): boolean {
    const predicted = candidate.predicted as JsonRecord;
    const ttftTarget = workload.ttftMs == null ? null : finiteNumber(workload.ttftMs);
    const tpotTarget = workload.tpotMs == null ? null : finiteNumber(workload.tpotMs);
    return candidate.deployable === true
        && Number(candidate.decodeTp) > 0 && Number(candidate.decodeReplicas) > 0
        && (candidate.topologyMode !== 'disagg'
            || (Number(candidate.prefillTp) > 0 && Number(candidate.prefillReplicas) > 0))
        && (ttftTarget === null || Number(predicted.ttftMs) > 0)
        && (tpotTarget === null || Number(predicted.tpotMs) > 0)
        && (ttftTarget !== null || tpotTarget !== null
            || Number(predicted.throughputTps) > 0);
}

function aicViolation(candidate: JsonRecord, workload: JsonRecord): number {
    const predicted = candidate.predicted as JsonRecord;
    return Math.max(
        workload.ttftMs == null ? 0 : Number(predicted.ttftMs) / Number(workload.ttftMs),
        workload.tpotMs == null ? 0 : Number(predicted.tpotMs) / Number(workload.tpotMs),
    );
}

candidateSearchRouter.post('/api/candidate-search/relaxed', async (req, res) => {
    try {
        const body = req.body && typeof req.body === 'object' ? req.body as JsonRecord : {};
        const workload = body.workload && typeof body.workload === 'object' ? body.workload as JsonRecord : {};
        const searchConfig = body.searchConfig && typeof body.searchConfig === 'object' ? body.searchConfig as JsonRecord : {};
        if (!String(workload.model || '').trim() || !Number.isInteger(searchConfig.totalGpus)
            || Number(searchConfig.totalGpus) < 1 || Number(searchConfig.totalGpus) > 64) {
            return res.status(400).json({error: 'Model and explicit totalGpus (1-64) are required'});
        }
        const targets = [workload.ttftMs, workload.tpotMs];
        if (targets.some(value => value != null && (typeof value !== 'number' || !Number.isFinite(value) || value <= 0))) {
            return res.status(400).json({error: 'Latency targets must be positive finite numbers'});
        }
        const payload = predictPayload(workload, searchConfig);
        const support = await aicFetch('/api/v1/aic/support', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
        });
        if (support.supported === false) return res.json({candidates: [], attempts: [], anchor: null, sloSatisfied: false, reason: 'unsupported'});
        const factors = targets.some(value => value !== undefined && value !== null) ? [1, 1.25, 1.5, 1.75, 2] : [1];
        const attempts: JsonRecord[] = [];
        const candidates: JsonRecord[] = [];
        const budget = Number(searchConfig.totalGpus);
        for (const factor of factors) {
            const request = {...payload};
            if (request.ttft_target_ms !== null) request.ttft_target_ms = Number(request.ttft_target_ms) * factor;
            if (request.tpot_target_ms !== null) request.tpot_target_ms = Number(request.tpot_target_ms) * factor;
            let result: JsonRecord;
            try {
                result = await aicFetch('/api/v1/aic/search', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(request),
                });
            } catch (error) {
                if (!(error instanceof Error && 'noFeasible' in error && error.noFeasible === true)) throw error;
                attempts.push({factor, ttftMs: request.ttft_target_ms ?? null, tpotMs: request.tpot_target_ms ?? null,
                    resultCount: 0, completeCount: 0});
                continue;
            }
            const configs = Array.isArray(result.configs) ? result.configs : [];
            const normalized = configs
                .filter(raw => {
                    const config = raw as JsonRecord;
                    return (config.mode === 'agg' && Number(config.tp) > 0 && Number(config.replicas) > 0)
                        || (config.mode === 'disagg' && Number(config.prefill_tp) > 0
                            && Number(config.prefill_replicas) > 0 && Number(config.decode_tp) > 0
                            && Number(config.decode_replicas) > 0);
                })
                .map((raw, index) => normalizeAicCandidate(raw as JsonRecord, index))
                .filter(candidate => completeAicCandidate(candidate, workload)
                    && (Number(candidate.decodeTp) * Number(candidate.decodeReplicas)
                        + Number(candidate.prefillTp) * Number(candidate.prefillReplicas)) <= budget)
                .slice(0, positiveInteger(searchConfig.maxCandidatesPerMode, 10, 50));
            candidates.push(...normalized);
            attempts.push({factor, ttftMs: request.ttft_target_ms ?? null, tpotMs: request.tpot_target_ms ?? null,
                resultCount: configs.length, completeCount: normalized.length});
            if (normalized.length) break;
        }
        candidates.sort((left, right) => aicViolation(left, workload) - aicViolation(right, workload)
            || Number(left.totalGpus) - Number(right.totalGpus));
        const anchor = candidates[0] || null;
        return res.json({candidates, attempts, anchor, sloSatisfied: anchor !== null && aicViolation(anchor, workload) <= 1,
            reason: anchor ? null : 'no_complete_anchor', backend: 'local-aic'});
    } catch (error) {
        const status = typeof error === 'object' && error && 'status' in error ? Number(error.status) : 502;
        const message = error instanceof Error ? error.message : 'AIC candidate search failed';
        return res.status(status >= 400 && status < 600 ? status : 502).json({error: message});
    }
});

function normalizeManualCandidate(raw: JsonRecord, index: number, label: string): JsonRecord {
    const mode = String(raw.mode || raw.serving_mode || 'disagg');
    const isAggregated = mode === 'agg';
    const prefillTp = isAggregated ? 0 : positiveInteger(raw.prefill_tp, 1);
    const prefillReplicas = isAggregated ? 0 : positiveInteger(raw.prefill_replicas, 1);
    const decodeTp = isAggregated ? positiveInteger(raw.tp, 1) : positiveInteger(raw.decode_tp, 1);
    const pipelineParallel = isAggregated ? positiveInteger(raw.pp, 1) : 1;
    const reportedGpus = finiteNumber(raw.num_total_gpus);
    const inferredReplicas = reportedGpus && isAggregated
        ? Math.max(1, Math.floor(reportedGpus / (decodeTp * pipelineParallel)))
        : 1;
    const decodeReplicas = isAggregated ? positiveInteger(raw.replicas, inferredReplicas) : positiveInteger(raw.decode_replicas, 1);
    const computedGpus = prefillTp * prefillReplicas + decodeTp * decodeReplicas;
    const totalGpus = positiveInteger(raw.num_total_gpus, computedGpus);
    const rawName = String(raw.name || raw.experiment_name || `manual-${index + 1}`);
    return {
        ...candidateBase('manual', rawName.replace(/[^a-z0-9_.-]+/gi, '-'), index),
        name: rawName,
        sourceLabel: label,
        family: 'Existing / User',
        topologyMode: mode,
        prefillTp,
        prefillReplicas,
        decodeTp,
        decodeReplicas,
        pipelineParallel,
        encodeTp: mode === 'epd' ? positiveInteger(raw.encode_tp, 1) : null,
        encodeReplicas: mode === 'epd' ? positiveInteger(raw.encode_replicas, 1) : null,
        totalGpus,
        predicted: {
            ttftMs: finiteNumber(raw.ttft_ms),
            tpotMs: finiteNumber(raw.tpot_ms),
            throughputTps: finiteNumber(raw.throughput_tokens_per_sec),
        },
        deployable: mode !== 'epd',
        deploymentNote: mode === 'epd' ? 'The current deployment backend does not support E/P/D topology.' : null,
        isChosenExperiment: raw.is_chosen === true,
    };
}

function normalizeGridCandidate(source: string, raw: JsonRecord, index: number): JsonRecord {
    const isBaseline = source === 'baseline' || source === 'tiered-cache';
    const prefillTp = isBaseline ? 0 : positiveInteger(raw.prefill_tp, 1);
    const prefillReplicas = isBaseline ? 0 : positiveInteger(raw.prefill_replicas, 1);
    const decodeTp = isBaseline ? positiveInteger(raw.tensor_parallel ?? raw.tp, 1) : positiveInteger(raw.decode_tp, 1);
    const decodeReplicas = isBaseline ? positiveInteger(raw.replicas, 1) : positiveInteger(raw.decode_replicas, 1);
    const encodeTp = source === 'epd' ? positiveInteger(raw.encode_tp, 1) : null;
    const encodeReplicas = source === 'epd' ? positiveInteger(raw.encode_replicas, 1) : null;
    const computedGpus = (encodeTp || 0) * (encodeReplicas || 0)
        + prefillTp * prefillReplicas + decodeTp * decodeReplicas;
    const totalGpus = positiveInteger(raw.total_gpus_used, computedGpus);
    const rawConfigId = String(raw.config_id || `${source}-${index + 1}`);
    const configId = source === 'tiered-cache' ? rawConfigId.replace(/^baseline-/, 'tpc-') : rawConfigId;
    return {
        ...candidateBase(source, configId, index),
        name: configId,
        topologyMode: source === 'baseline' || source === 'tiered-cache' ? 'agg' : source,
        prefillTp,
        prefillReplicas,
        decodeTp,
        decodeReplicas,
        encodeTp,
        encodeReplicas,
        totalGpus,
        predicted: null,
        deployable: source !== 'epd',
        deploymentNote: source === 'epd' ? 'The current deployment backend does not support E/P/D topology.' : null,
    };
}

function selectConfigs(configs: unknown, limit: number): JsonRecord[] {
    if (!Array.isArray(configs)) return [];
    return configs
        .filter((item): item is JsonRecord => Boolean(item && typeof item === 'object'))
        .sort((left, right) => Number(right.total_gpus_used || 0) - Number(left.total_gpus_used || 0)
            || String(left.config_id || '').localeCompare(String(right.config_id || '')))
        .slice(0, limit);
}

function generateEpdConfigs(totalGpus: number): JsonRecord[] {
    const configs: JsonRecord[] = [];
    for (const encodeTp of [1, 2]) {
        for (const prefillTp of [1, 2]) {
            for (const decodeTp of [1, 2]) {
                for (let encodeReplicas = 1; encodeReplicas <= 2; encodeReplicas += 1) {
                    for (let prefillReplicas = 1; prefillReplicas <= 2; prefillReplicas += 1) {
                        for (let decodeReplicas = 1; decodeReplicas <= 3; decodeReplicas += 1) {
                            const totalGpusUsed = encodeTp * encodeReplicas
                                + prefillTp * prefillReplicas + decodeTp * decodeReplicas;
                            if (totalGpusUsed <= totalGpus) {
                                configs.push({
                                    type: 'epd',
                                    guide: 'epd-disaggregation',
                                    encode_tp: encodeTp,
                                    encode_replicas: encodeReplicas,
                                    prefill_tp: prefillTp,
                                    prefill_replicas: prefillReplicas,
                                    decode_tp: decodeTp,
                                    decode_replicas: decodeReplicas,
                                    total_gpus_used: totalGpusUsed,
                                    config_id: `epd-e${encodeTp}x${encodeReplicas}-p${prefillTp}x${prefillReplicas}-d${decodeTp}x${decodeReplicas}`,
                                });
                            }
                        }
                    }
                }
            }
        }
    }
    return configs;
}

function generateGridConfigs(source: string, totalGpus: number): JsonRecord[] {
    if (source === 'epd') return generateEpdConfigs(totalGpus);
    const configs: JsonRecord[] = [];
    if (source === 'baseline' || source === 'tiered-cache') {
        for (const tensorParallel of [1, 2, 4, 8]) {
            for (let replicas = 1; replicas <= totalGpus; replicas += 1) {
                const used = tensorParallel * replicas;
                if (used <= totalGpus) configs.push({
                    tensor_parallel: tensorParallel,
                    replicas,
                    total_gpus_used: used,
                    config_id: `baseline-tp${tensorParallel}-rep${replicas}`,
                });
            }
        }
        return configs;
    }
    for (const prefillTp of [1, 2, 4]) {
        for (const decodeTp of [1, 2, 4]) {
            for (let prefillReplicas = 1; prefillReplicas <= totalGpus; prefillReplicas += 1) {
                for (let decodeReplicas = 1; decodeReplicas <= totalGpus; decodeReplicas += 1) {
                    const used = prefillTp * prefillReplicas + decodeTp * decodeReplicas;
                    if (used <= totalGpus) configs.push({
                        prefill_tp: prefillTp,
                        prefill_replicas: prefillReplicas,
                        decode_tp: decodeTp,
                        decode_replicas: decodeReplicas,
                        total_gpus_used: used,
                        config_id: `pd-p${prefillTp}x${prefillReplicas}-d${decodeTp}x${decodeReplicas}`,
                    });
                }
            }
        }
    }
    return configs;
}

candidateSearchRouter.post('/api/candidate-support', async (req, res) => {
    try {
        const body = req.body && typeof req.body === 'object' ? req.body as JsonRecord : {};
        const workload = body.workload && typeof body.workload === 'object' ? body.workload as JsonRecord : {};
        const searchConfig = body.searchConfig && typeof body.searchConfig === 'object' ? body.searchConfig as JsonRecord : {};
        const payload = predictPayload(workload, searchConfig, await loadHardwareProfiles());
        if (!payload.model_name) return res.status(400).json({ error: 'Model is required for the AIC support check' });
        if (!payload.aic_system_name) return res.status(400).json({ error: 'AIC system is required for the support check' });
        const support = await aicFetch('/api/v1/aic/support', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        return res.json(support);
    } catch (error) {
        const status = typeof error === 'object' && error && 'status' in error ? Number(error.status) : 502;
        const message = error instanceof Error ? error.message : 'AIC support check failed';
        console.error('[Candidate Support]', message);
        return res.status(status >= 400 && status < 600 ? status : 502).json({ error: message });
    }
});

candidateSearchRouter.post('/api/candidate-search', async (req, res) => {
    try {
        const body = req.body && typeof req.body === 'object' ? req.body as JsonRecord : {};
        const workload = body.workload && typeof body.workload === 'object' ? body.workload as JsonRecord : {};
        const searchConfig = body.searchConfig && typeof body.searchConfig === 'object' ? body.searchConfig as JsonRecord : {};
        const manualConfig = body.manualConfig && typeof body.manualConfig === 'object' ? body.manualConfig as JsonRecord : {};
        const requestedSources = Array.isArray(body.sourceIds) ? body.sourceIds.map(String) : [];
        const sourceIds = [...new Set(requestedSources.filter((source) => source === 'aic' || source === 'manual' || SEARCH_SOURCES.has(source)))];
        if (!sourceIds.length) return res.json({ candidates: [] });

        const totalGpus = positiveInteger(searchConfig.totalGpus, 8, 64);
        const maxCandidatesPerMode = positiveInteger(searchConfig.maxCandidatesPerMode, 10, 50);
        const aicSystemName = String(searchConfig.aicSystemName || 'b60').trim();
        const aicBackendName = String(searchConfig.aicBackendName || 'vllm').trim().toLowerCase();
        const candidates: JsonRecord[] = [];
        const hardwareProfiles = await loadHardwareProfiles();

        if (sourceIds.includes('aic')) {
            if (!aicSystemName) throw Object.assign(new Error('AIC system is required for Predictive search'), { status: 400 });
            const payload = predictPayload(workload, searchConfig, hardwareProfiles);
            const support = await aicFetch('/api/v1/aic/support', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (support.supported === false) {
                throw Object.assign(new Error(`${String(workload.model || 'Model')} is not supported on ${aicSystemName} with ${aicBackendName}`), { status: 400 });
            }
            const prediction = await aicFetch('/api/v1/aic/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const configs = Array.isArray(prediction.configs) ? prediction.configs : [];
            candidates.push(...configs.slice(0, maxCandidatesPerMode).map((raw, index) => normalizeAicCandidate(raw as JsonRecord, index)));
        }

        if (sourceIds.includes('manual')) {
            const inputMode = String(manualConfig.inputMode || 'form');
            if (inputMode === 'yaml') {
                const yamlText = String(manualConfig.yamlText || '').trim();
                if (!yamlText) throw Object.assign(new Error('Upload an AIConfigurator experiment YAML file first'), { status: 400 });
                const result = await aicFetch('/api/v1/aic/experiments', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ yaml_text: yamlText, top_n: maxCandidatesPerMode }),
                });
                const experiments = Array.isArray(result.experiments) ? result.experiments : [];
                candidates.push(...experiments.map((raw, index) => normalizeManualCandidate(raw as JsonRecord, index, 'YAML Experiment')));
            } else {
                if (!aicSystemName) throw Object.assign(new Error('AIC system is required for Manual Estimate'), { status: 400 });
                const mode = String(manualConfig.servingMode || 'disagg');
                const tp = positiveInteger(manualConfig.tp, 1);
                const pp = positiveInteger(manualConfig.pp, 1);
                const replicas = positiveInteger(manualConfig.replicas, 1);
                const prefillTp = positiveInteger(manualConfig.prefillTp, 1);
                const prefillReplicas = positiveInteger(manualConfig.prefillReplicas, 1);
                const decodeTp = positiveInteger(manualConfig.decodeTp, 1);
                const decodeReplicas = positiveInteger(manualConfig.decodeReplicas, 1);
                const payload = {
                    ...predictPayload(workload, searchConfig, hardwareProfiles),
                    scenario: mode === 'agg' ? 'inference_scheduling' : 'pd_disaggregation',
                    gpu_count: mode === 'agg' ? tp * pp * replicas : prefillTp * prefillReplicas + decodeTp * decodeReplicas,
                    tp, pp, replicas,
                    batch_size: positiveInteger(manualConfig.batchSize, 128, 1_000_000),
                    prefill_tp: prefillTp,
                    prefill_replicas: prefillReplicas,
                    prefill_batch_size: positiveInteger(manualConfig.prefillBatchSize, 1, 1_000_000),
                    decode_tp: decodeTp,
                    decode_replicas: decodeReplicas,
                    decode_batch_size: positiveInteger(manualConfig.decodeBatchSize, 64, 1_000_000),
                };
                const support = await aicFetch('/api/v1/aic/support', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const modeSupported = mode === 'agg' ? support.agg_supported : support.disagg_supported;
                if (modeSupported === false) {
                    throw Object.assign(new Error(`${mode === 'agg' ? 'Aggregated' : 'P/D disaggregated'} serving is not supported for this model and hardware`), { status: 400 });
                }
                const estimate = await aicFetch('/api/v1/aic/estimate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                candidates.push(normalizeManualCandidate({ ...estimate, name: String(manualConfig.name || 'manual-estimate') }, 0, 'Manual Estimate'));
            }
        }

        const gridSources = sourceIds.filter((source) => SEARCH_SOURCES.has(source));
        if (gridSources.length) {
            for (const source of gridSources) {
                const selected = selectConfigs(generateGridConfigs(source, totalGpus), maxCandidatesPerMode);
                candidates.push(...selected.map((raw, index) => normalizeGridCandidate(source, raw, index)));
            }
        }

        return res.json({ candidates, backend: 'local-aic' });
    } catch (error) {
        const status = typeof error === 'object' && error && 'status' in error ? Number(error.status) : 502;
        const message = error instanceof Error ? error.message : 'AIC candidate search failed';
        console.error('[Candidate Search]', message);
        return res.status(status >= 400 && status < 600 ? status : 502).json({ error: message });
    }
});
