import assert from "node:assert";
import { guideResultProfile, SUPPORTED_BENCHMARK_GUIDES } from "./guideProfiles.js";

assert.deepStrictEqual(SUPPORTED_BENCHMARK_GUIDES, [
    "optimized-baseline", "pd-disaggregation", "precise-prefix-cache-routing", "tiered-prefix-cache",
]);
for (const guide of SUPPORTED_BENCHMARK_GUIDES) {
    const profile = guideResultProfile(guide);
    assert.ok(profile.label);
    assert.ok(profile.primaryMetrics.length >= 2);
    assert.ok(profile.arms.length >= 2);
    assert.ok(profile.mechanism.length >= 10);
    assert.ok(profile.system.length >= 8);
    assert.ok(profile.deployment.length >= 4);
    assert.ok(profile.diagnosis.includes("Metrics Missing"));
    assert.ok(profile.reportSection);
}

console.log("guide result profile tests passed");
