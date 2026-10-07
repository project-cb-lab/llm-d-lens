import assert from "node:assert/strict";
import test from "node:test";

import {
    ACTIVE_EVALUATION_STATUSES,
    TERMINAL_EVALUATION_STATUSES,
    comparisonName,
    evaluationStatusTone,
    isTerminalEvaluationStatus,
} from "./domain.js";

test("classifies evaluation lifecycle states", () => {
    assert.equal(ACTIVE_EVALUATION_STATUSES.has("deploying"), true);
    assert.equal(TERMINAL_EVALUATION_STATUSES.has("succeeded"), true);
    assert.equal(isTerminalEvaluationStatus("cleaned"), true);
    assert.equal(isTerminalEvaluationStatus("benchmarking"), false);
});

test("uses stable labels and safely falls back for unknown comparison types", () => {
    assert.equal(comparisonName("kubernetes-service"), "Kubernetes Service round-robin");
    assert.equal(comparisonName("future-provider"), "future-provider");
});

test("maps lifecycle states to semantic visual tones", () => {
    assert.match(evaluationStatusTone("succeeded"), /emerald/);
    assert.match(evaluationStatusTone("failed"), /red/);
    assert.match(evaluationStatusTone("deploying"), /cyan/);
    assert.match(evaluationStatusTone("queued"), /amber/);
});

test('cancellation is active until cleanup finishes; deployment readiness is not benchmark completion', () => {
    assert.equal(isTerminalEvaluationStatus('cancelling'), false);
    assert.equal(isTerminalEvaluationStatus('ready'), false);
    for (const status of ['succeeded', 'failed', 'cancelled', 'cleaned']) {
        assert.equal(isTerminalEvaluationStatus(status), true);
    }
});
