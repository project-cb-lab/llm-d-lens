import test from "node:test";
import assert from "node:assert/strict";

import { applyRecommendedWorkload, benchmarkWorkloadMode, defaultGuideVariant, requiredBaselineTypes } from "./capabilities.js";

test("guide defaults are read from capability metadata", () => {
    const provider = {
        variants: ["base", "cpu"],
        evaluation: {
            default_variant: "cpu",
            required_baselines: ["direct-vllm", "direct-vllm"],
            recommended_workload: { kind: "shared-prefix", shared_prefix: { num_groups: 4 } },
        },
    };
    assert.equal(defaultGuideVariant(provider), "cpu");
    assert.deepEqual(requiredBaselineTypes(provider), ["direct-vllm"]);
    assert.deepEqual(applyRecommendedWorkload({ matrix: [{ isl: 1, osl: 1 }] }, provider), {
        matrix: [], concurrency_stages: [], shared_prefix: { num_groups: 4 }, workload_yaml: null,
    });
});

test("unknown guides retain safe generic defaults", () => {
    assert.equal(defaultGuideVariant({ variants: ["base"] }), "base");
    assert.deepEqual(requiredBaselineTypes({}), []);
});

test("benchmark helpers tolerate an unset benchmark during configuration changes", () => {
    assert.equal(benchmarkWorkloadMode(null), "profile");
    assert.equal(benchmarkWorkloadMode({ shared_prefix: {} }), "shared-prefix");
    assert.equal(benchmarkWorkloadMode({ matrix: [{ isl: 1, osl: 1 }] }), "matrix");
    assert.equal(benchmarkWorkloadMode({ workload_yaml: "load: {}" }), "yaml");
    assert.deepEqual(applyRecommendedWorkload(null, null), {
        matrix: [], concurrency_stages: [], shared_prefix: null, workload_yaml: null,
    });
});
