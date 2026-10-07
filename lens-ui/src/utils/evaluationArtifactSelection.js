export function returnedEvaluationArtifactIds(plans, fallbackArtifactId = "") {
    if (!Array.isArray(plans)) return [];
    return [...new Set(
        plans
            .map((plan) => plan?.configuration_artifact_id || fallbackArtifactId)
            .filter(Boolean),
    )];
}
