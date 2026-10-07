export const EVALUATION_STATUS = Object.freeze({
    QUEUED: "queued",
    RUNNING: "running",
    CANCELLING: "cancelling",
    RENDERING: "rendering",
    DEPLOYING: "deploying",
    BENCHMARKING: "benchmarking",
    READY: "ready",
    SUCCEEDED: "succeeded",
    FAILED: "failed",
    CANCELLED: "cancelled",
    STOPPED: "stopped",
    CLEANED: "cleaned",
});

export const ACTIVE_EVALUATION_STATUSES = new Set([
    EVALUATION_STATUS.QUEUED,
    EVALUATION_STATUS.RUNNING,
    EVALUATION_STATUS.CANCELLING,
    EVALUATION_STATUS.RENDERING,
    EVALUATION_STATUS.DEPLOYING,
    EVALUATION_STATUS.BENCHMARKING,
]);

export const TERMINAL_EVALUATION_STATUSES = new Set([
    EVALUATION_STATUS.SUCCEEDED,
    EVALUATION_STATUS.FAILED,
    EVALUATION_STATUS.CANCELLED,
    EVALUATION_STATUS.CLEANED,
]);

const BASELINE_LABELS = Object.freeze({
    "direct-vllm": "Prism plain vLLM reference",
    "router-neutral": "Prism neutral-router reference",
    "router-round-robin": "Prism neutral-router reference",
    "load-only": "Load-only routing",
    "affinity-only": "Affinity Policy Only",
    "optimized-baseline": "llm-d optimized-baseline Guide",
    "kubernetes-service": "Kubernetes Service round-robin",
});

export function comparisonName(type) {
    return BASELINE_LABELS[type] || type || "Unknown comparison";
}

export function isTerminalEvaluationStatus(status) {
    return TERMINAL_EVALUATION_STATUSES.has(status);
}

const DISPLAY_STATUS = Object.freeze({
    queued: "Preparing",
    running: "Preparing",
    rendering: "Preparing",
    deploying: "Deploying",
    ready: "Validating",
    benchmarking: "Benchmarking",
    analyzing: "Analyzing",
    succeeded: "Completed",
    failed: "Needs Attention",
    cancelling: "Stopping",
    cancelled: "Stopped",
    stopped: "Stopped",
    cleaned: "Stopped / Cleaned",
});

export function evaluationStatusLabel(status) {
    return DISPLAY_STATUS[status] || status || "Unknown";
}

export function evaluationStatusTone(status) {
    if ([EVALUATION_STATUS.SUCCEEDED, EVALUATION_STATUS.READY].includes(status)) {
        return "border-emerald-500/30 bg-emerald-500/10 text-emerald-200";
    }
    if ([EVALUATION_STATUS.FAILED, EVALUATION_STATUS.CANCELLED].includes(status)) {
        return "border-red-500/30 bg-red-500/10 text-red-200";
    }
    if (ACTIVE_EVALUATION_STATUSES.has(status) && status !== EVALUATION_STATUS.QUEUED) {
        return "border-cyan-500/30 bg-cyan-500/10 text-cyan-200";
    }
    return "border-amber-500/30 bg-amber-500/10 text-amber-200";
}
