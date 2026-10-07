import test from "node:test";
import assert from "node:assert/strict";

import { returnedEvaluationArtifactIds } from "./evaluationArtifactSelection.js";

test("uses only artifacts returned by the latest configuration publish", () => {
    const plans = [
        { configuration_artifact_id: "tiered-base" },
        { configuration_artifact_id: "tiered-native-cpu" },
    ];

    assert.deepEqual(
        returnedEvaluationArtifactIds(plans, "tiered-base"),
        ["tiered-base", "tiered-native-cpu"],
    );
});

test("uses the returned artifact as a fallback without duplicates", () => {
    assert.deepEqual(
        returnedEvaluationArtifactIds([{}, {}], "tiered-base"),
        ["tiered-base"],
    );
});
