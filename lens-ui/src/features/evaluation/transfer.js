export const EVALUATION_TRANSFER_KEYS = Object.freeze({
    INTENT: "prism_evaluate_pending_intent",
    PREFILL: "prism_evaluate_prefill_workloads",
    ARTIFACT: "prism_evaluate_configuration_artifact",
    SIMULATION_BENCHMARKS: "prism_simulate_benchmarks",
});

function readJson(storage, key, { remove = false } = {}) {
    const raw = storage.getItem(key);
    if (remove) storage.removeItem(key);
    if (!raw) return null;
    try {
        return JSON.parse(raw);
    } catch {
        return null;
    }
}

function writeJson(storage, key, value) {
    if (value === undefined || value === null) {
        storage.removeItem(key);
        return;
    }
    storage.setItem(key, JSON.stringify(value));
}

export function consumeEvaluationTransfer(storage = sessionStorage) {
    return {
        intent: readJson(storage, EVALUATION_TRANSFER_KEYS.INTENT, { remove: true }),
        plans: readJson(storage, EVALUATION_TRANSFER_KEYS.PREFILL, { remove: true }),
        artifact: readJson(storage, EVALUATION_TRANSFER_KEYS.ARTIFACT, { remove: true }),
    };
}

export function storeEvaluationIntent(intent, storage = sessionStorage) {
    writeJson(storage, EVALUATION_TRANSFER_KEYS.INTENT, intent);
}

export function readEvaluationIntent(storage = sessionStorage) {
    return readJson(storage, EVALUATION_TRANSFER_KEYS.INTENT);
}

export function clearEvaluationIntent(storage = sessionStorage) {
    storage.removeItem(EVALUATION_TRANSFER_KEYS.INTENT);
}

export function storeEvaluationTransfer({ intent, plans, artifact }, storage = sessionStorage) {
    if (intent !== undefined) writeJson(storage, EVALUATION_TRANSFER_KEYS.INTENT, intent);
    if (plans !== undefined) writeJson(storage, EVALUATION_TRANSFER_KEYS.PREFILL, plans);
    if (artifact !== undefined) writeJson(storage, EVALUATION_TRANSFER_KEYS.ARTIFACT, artifact);
}

export function readEvaluationPlans(storage = sessionStorage, { consume = false } = {}) {
    return readJson(storage, EVALUATION_TRANSFER_KEYS.PREFILL, { remove: consume });
}

export function readEvaluationArtifact(storage = sessionStorage) {
    return readJson(storage, EVALUATION_TRANSFER_KEYS.ARTIFACT);
}

export function storeEvaluationArtifact(artifact, storage = sessionStorage) {
    writeJson(storage, EVALUATION_TRANSFER_KEYS.ARTIFACT, artifact);
}

export function storeSimulationBenchmarks(benchmarks, storage = sessionStorage) {
    writeJson(storage, EVALUATION_TRANSFER_KEYS.SIMULATION_BENCHMARKS, benchmarks);
}
