import assert from "node:assert/strict";
import test from "node:test";

import {
    EVALUATION_TRANSFER_KEYS,
    clearEvaluationIntent,
    consumeEvaluationTransfer,
    readEvaluationArtifact,
    readEvaluationIntent,
    readEvaluationPlans,
    storeEvaluationArtifact,
    storeEvaluationIntent,
    storeEvaluationTransfer,
    storeSimulationBenchmarks,
} from "./transfer.js";

function memoryStorage() {
    const values = new Map();
    return {
        getItem: (key) => values.get(key) ?? null,
        removeItem: (key) => values.delete(key),
        setItem: (key, value) => values.set(key, value),
    };
}

test("consumes evaluation transfer values exactly once", () => {
    const storage = memoryStorage();
    storage.setItem(EVALUATION_TRANSFER_KEYS.PREFILL, JSON.stringify([{ id: "plan" }]));
    storage.setItem(EVALUATION_TRANSFER_KEYS.ARTIFACT, JSON.stringify({ artifact_id: "artifact" }));
    assert.deepEqual(consumeEvaluationTransfer(storage), {
        intent: null,
        plans: [{ id: "plan" }],
        artifact: { artifact_id: "artifact" },
    });
    assert.deepEqual(consumeEvaluationTransfer(storage), { intent: null, plans: null, artifact: null });
});

test("stores typed navigation payloads behind stable keys", () => {
    const storage = memoryStorage();
    storeEvaluationIntent({ operation: "create-evaluation" }, storage);
    storeSimulationBenchmarks([{ id: "benchmark" }], storage);
    assert.deepEqual(JSON.parse(storage.getItem(EVALUATION_TRANSFER_KEYS.INTENT)), { operation: "create-evaluation" });
    assert.deepEqual(JSON.parse(storage.getItem(EVALUATION_TRANSFER_KEYS.SIMULATION_BENCHMARKS)), [{ id: "benchmark" }]);
});

test("discards malformed transfer JSON without breaking navigation", () => {
    const storage = memoryStorage();
    storage.setItem(EVALUATION_TRANSFER_KEYS.INTENT, "not-json");
    assert.equal(consumeEvaluationTransfer(storage).intent, null);
});

test("supports symmetric transfer reads, writes, consumption, and clearing", () => {
    const storage = memoryStorage();
    storeEvaluationTransfer({
        intent: { name: "evaluation" },
        plans: [{ id: "plan" }],
        artifact: { artifact_id: "artifact" },
    }, storage);
    assert.deepEqual(readEvaluationIntent(storage), { name: "evaluation" });
    assert.deepEqual(readEvaluationPlans(storage), [{ id: "plan" }]);
    assert.deepEqual(readEvaluationPlans(storage, { consume: true }), [{ id: "plan" }]);
    assert.equal(readEvaluationPlans(storage), null);
    assert.deepEqual(readEvaluationArtifact(storage), { artifact_id: "artifact" });
    clearEvaluationIntent(storage);
    assert.equal(readEvaluationIntent(storage), null);
    storeEvaluationArtifact(null, storage);
    assert.equal(readEvaluationArtifact(storage), null);
});
