export function evaluationCapability(provider) {
    return provider?.evaluation || {};
}

export function defaultGuideVariant(provider, modelServer) {
    const variants = provider?.variants?.length ? provider.variants : modelServer?.variants || [];
    const preferred = evaluationCapability(provider).default_variant;
    if (preferred && variants.includes(preferred)) return preferred;
    return variants[0] === "." ? "" : variants[0] || "";
}

export function requiredBaselineTypes(provider) {
    return [...new Set(evaluationCapability(provider).required_baselines || [])];
}

export function applyRecommendedWorkload(current, provider) {
    const base = current && typeof current === "object" ? current : {};
    const recommended = evaluationCapability(provider).recommended_workload || {};
    return {
        ...base,
        matrix: recommended.kind === "matrix" ? recommended.matrix || base.matrix || [] : [],
        concurrency_stages: recommended.kind === "matrix"
            ? recommended.concurrency_stages || base.concurrency_stages || []
            : [],
        shared_prefix: recommended.kind === "shared-prefix"
            ? recommended.shared_prefix || base.shared_prefix || null
            : null,
        workload_yaml: null,
    };
}

export function benchmarkWorkloadMode(benchmark) {
    if (benchmark?.shared_prefix) return "shared-prefix";
    if (benchmark?.matrix?.length) return "matrix";
    if (benchmark?.workload_yaml) return "yaml";
    return "profile";
}

export function configurationSummary(artifact, provider) {
    const configuration = artifact?.deployable_configuration || {};
    const content = configuration.content || {};
    const serving = content.decode || content.serving || {};
    const prefill = content.prefill || {};
    const variant = content.guideVariant || content.officialGuide?.source?.variant || "";
    return {
        guide: provider?.label || configuration.provider_ref || configuration.type || "Configuration",
        model: content.model?.name || "Unknown model",
        topology: prefill.replicaCount != null
            ? `${prefill.replicaCount}P×TP${prefill.tensorParallelSize ?? "—"} / ${serving.replicaCount ?? "—"}D×TP${serving.tensorParallelSize ?? "—"}`
            : `${serving.replicaCount ?? "—"} replica · TP${serving.tensorParallelSize ?? "—"}`,
        candidate: provider?.variant_labels?.[variant] || variant || "Default Guide topology",
        experimentVariable: evaluationCapability(provider).experiment_variable || "deployment configuration",
    };
}
