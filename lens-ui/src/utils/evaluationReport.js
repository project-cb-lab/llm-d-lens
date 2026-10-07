const METRICS = [
    ["throughput_tps", "Peak output tokens/s"],
    ["throughput_rps", "Requests/sec"],
    ["ttft_ms", "Mean TTFT (ms)"],
    ["itl_ms", "Mean ITL (ms)"],
    ["tpot_ms", "Mean TPOT (ms)"],
    ["request_latency_ms", "Mean end-to-end (ms)"],
    ["success_rate", "Success rate (%)"],
    ["error_rate", "Error rate (%)"],
];

function value(value) {
    if (typeof value !== "number" || Number.isNaN(value)) return "—";
    return Number.isInteger(value) ? value.toLocaleString("en-US") : value.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function delta(value) {
    if (typeof value !== "number" || Number.isNaN(value)) return "—";
    return `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
}

function escapeCell(input) {
    return String(input ?? "—").replaceAll("|", "\\|").replaceAll("\n", " ");
}

function evidenceValue(input, unit = "") {
    if (typeof input === "number") return `${value(input)}${unit ? ` ${unit}` : ""}`;
    if (typeof input === "string" || typeof input === "boolean") return String(input).replaceAll("_", " ");
    if (input && typeof input === "object" && (typeof input.value === "number" || typeof input.value === "string")) return evidenceValue(input.value, input.unit || unit);
    return input && typeof input === "object" ? escapeCell(JSON.stringify(input)) : "—";
}

function caseName(item) {
    const spec = item.configuration || item.spec || {};
    if (item.kind === "baseline") return item.baseline_type || "baseline";
    return spec.guide || item.deployment_configuration?.provider_ref || "Guide";
}

function ratioPercent(ratio) {
    return typeof ratio === "number" ? delta((ratio - 1) * 100) : "—";
}

function comparisonConfigurationName(facts, fallback) {
    const model = String(facts?.model || "").split("/").pop();
    const variant = facts?.guide_variant === "base" ? "VRAM-only" : facts?.guide_variant?.includes("cpu") ? "VRAM + CPU RAM" : null;
    return [variant || facts?.guide || fallback, model].filter(Boolean).join(" · ") || "—";
}

const GUIDE_REPORT_SECTIONS = {
    "optimized-baseline": {
        title: "Optimized Baseline routing evidence",
        architecture: "Approximate prefix history → saturation-aware affinity filter → token-load scorer → model-server endpoint",
        evidence: ["Prefix cache hit/locality", "Per-endpoint token load and queue pressure", "TTFT tail across offered load", "Stable output capacity", "Low-reuse negative control"],
        tradeoffs: "Affinity must be interpreted with load balance. A high cache-hit rate is not a win if sticky endpoints develop pathological queues.",
    },
    "precise-prefix-cache-routing": {
        title: "Precise Prefix Cache routing evidence",
        architecture: "KV events → precise per-pod block index → affinity filter → token-load scorer → cache-warm endpoint",
        evidence: ["Publisher/subscriber and index health", "Matched blocks and cached prompt tokens", "Recomputed prompt tokens", "Prefix-group routing locality", "Precise versus approximate routing"],
        tradeoffs: "Precise routing adds event, index, render/tokenization, and scheduling overhead. These must be smaller than the avoided prefill and queue time.",
    },
    "pd-disaggregation": {
        title: "P/D disaggregation evidence",
        architecture: "P/D decision → Prefill pool → KV handoff → Decode pool → streamed response",
        evidence: ["Role-correct P/D decisions", "KV transfer health", "Prefill and Decode pressure", "TTFT versus ITL/TPOT", "Resource-parity SLO goodput"],
        tradeoffs: "P/D can stabilize Decode ITL while increasing TTFT when Prefill receives less capacity. Report both latency dimensions and transfer evidence.",
    },
    "tiered-prefix-cache": {
        title: "Tiered Prefix Cache evidence",
        architecture: "HBM eviction → CPU/filesystem retention → restored KV → reduced repeated prefill",
        evidence: ["Working set versus HBM", "Offload/restore activity", "Effective cache hit", "TTFT and queue pressure", "Stable capacity"],
        tradeoffs: "Tier capacity benefit must be weighed against restore latency, transfer traffic, and host-memory consumption.",
    },
};

export function evaluationReportFilename(details) {
    const source = details?.workflow?.name || details?.workflow?.id || "evaluation";
    const slug = String(source).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 64);
    return `${slug || "evaluation"}-benchmark-report.md`;
}

export function buildEvaluationMarkdown(details, reportUrl = "") {
    const workflow = details?.workflow || {};
    const cases = (details?.cases || []).map((entry) => entry.case || entry).filter(Boolean);
    const report = details?.report || {};
    const comparisons = report.comparisons || [];
    const completed = cases.filter((item) => item.status === "succeeded");
    const failed = cases.filter((item) => ["failed", "cancelled"].includes(item.status));
    const firstBenchmark = cases.find((item) => item.benchmark || item.spec?.benchmark);
    const benchmark = firstBenchmark?.benchmark || firstBenchmark?.spec?.benchmark || {};
    const guideIds = [...new Set(cases.map((item) => (item.configuration || item.spec || {}).guide).filter(Boolean))];
    const lines = [
        `# ${workflow.name || "llm-d Benchmark Report"}`,
        "",
        `> Generated by llm-d Prism${report.generated_at ? ` on ${report.generated_at}` : ""}.`,
        "",
        "## Executive Summary",
        "",
        `- **Evaluation ID:** ${workflow.id || "—"}`,
        `- **Status:** ${workflow.status || "—"}`,
        `- **Created:** ${workflow.created_at || "—"}`,
        `- **Cases:** ${cases.length} total; ${completed.length} succeeded; ${failed.length} failed or cancelled`,
        `- **Benchmark runner:** ${benchmark.harness || "—"}`,
        `- **Traffic profile:** ${benchmark.workload || "—"}`,
        ...(reportUrl ? [`- **Interactive report:** ${reportUrl}`] : []),
        "",
        "## Test Environment & Software Versions",
        "",
        `- **Model:** ${(firstBenchmark?.configuration || firstBenchmark?.spec || {}).model || "—"}`,
        `- **Accelerator:** ${(firstBenchmark?.configuration || firstBenchmark?.spec || {}).accelerator || "—"}`,
        `- **Runtime:** ${(firstBenchmark?.configuration || firstBenchmark?.spec || {}).model_server || (firstBenchmark?.configuration || firstBenchmark?.spec || {}).runtime || "—"}`,
        `- **Deployment cases:** ${cases.length}`,
        "",
        "## Experiment Configuration & Comparison Arms",
        "",
        guideIds.length ? guideIds.map((guide) => `- ${guide}`).join("\n") : "Routing policy was not persisted.",
        "",
        "### Calibration",
        "",
        cases.some((item) => item.calibration) ? "Calibration records are included in the resolved case data below." : "Calibration evidence is unavailable. Repeat the run with calibration enabled before using calibration-dependent conclusions.",
        "",
        "## Benchmark Methodology & Workload",
        "",
        `- **Harness:** ${benchmark.harness || "—"}`,
        `- **Workload:** ${benchmark.workload || "—"}`,
        `- **Parallelism:** ${benchmark.parallelism || "—"}`,
        "",
        "### Test matrix",
        "",
        "| Configuration | Kind | Status | Model | Scenario |",
        "|---|---|---|---|---|",
        ...cases.map((item) => {
            const spec = item.configuration || item.spec || {};
            return `| ${escapeCell(caseName(item))} | ${escapeCell(item.kind)} | ${escapeCell(item.status)} | ${escapeCell(spec.model)} | ${escapeCell(item.scenario_name || item.scenario_id)} |`;
        }),
    ];

    if (comparisons.length) {
        lines.push(
            "", "## Policy Comparison", "", "## Comparison summary", "",
            "Each row compares a Guide with its selected baseline under the same workload. Positive throughput deltas are improvements; negative latency deltas are improvements.", "",
            `| Metric | ${comparisons.map((item) => escapeCell(`${item.guide || "Guide"} vs ${item.baseline_type || "baseline"}`)).join(" | ")} |`,
            `|---|${comparisons.map(() => "---|").join("")}`,
            ...METRICS.map(([key, label]) => `| ${label} | ${comparisons.map((item) => `${value(item.guide_metrics?.[key])} vs ${value(item.baseline_metrics?.[key])} (${delta(item.delta_percent?.[key])})`).join(" | ")} |`),
        );
        lines.push("", "### Comparison validity", "", "| Comparison | Model | Runtime | Accelerator type | Accelerator count | Workload | Valid |", "|---|---|---|---|---|---|---|");
        comparisons.forEach((item) => {
            const checks = item.parity?.checks || {};
            const mark = (key) => checks[key] === true ? "Yes" : checks[key] === false ? "No" : "Unknown";
            lines.push(`| ${escapeCell(`${item.guide || "Guide"} vs ${item.baseline_type || "baseline"}`)} | ${mark("model")} | ${mark("runtime")} | ${mark("accelerator_type")} | ${mark("accelerator_count")} | ${mark("workload")} | ${item.parity?.valid ? "Yes" : "No"} |`);
        });
    } else {
        lines.push("", "## Policy Comparison", "", "The required RR, load-only, affinity-only, and full optimized-baseline comparison matrix is incomplete.");
    }
    lines.push("", "## Deployment & Functional Validation", "", completed.length ? `${completed.length} benchmark case(s) completed successfully.` : "No completed benchmark cases were recorded.");

    comparisons.forEach((comparison, index) => {
        const stages = comparison.rate_stage_statistics?.rows || [];
        if (stages.length) {
            const guideName = comparisonConfigurationName(comparison.guide_configuration, comparison.guide || "Guide");
            const baselineName = comparisonConfigurationName(comparison.baseline_configuration, comparison.baseline_type || "Baseline");
            lines.push(
                "", ...(index === 0 ? ["## Performance & Capacity Results", ""] : []), `## Shared-prefix rate ladder${comparisons.length > 1 ? ` — ${comparison.guide || `comparison ${index + 1}`}` : ""}`, "",
                `Comparison: **${escapeCell(guideName)}** versus **${escapeCell(baselineName)}**.`, "",
                "| Target rate | Configuration | Mean TTFT (ms) | P90 TTFT (ms) | Mean E2E (ms) | P90 E2E (ms) | Throughput (tok/s) |",
                "|---:|---|---:|---:|---:|---:|---:|",
                ...stages.flatMap((row) => [
                    `| ${value(row.rate)} | ${escapeCell(baselineName)} | ${value(row.baseline?.ttft_ms)} | ${value(row.baseline_p90?.ttft_ms)} | ${value(row.baseline?.request_latency_ms)} | ${value(row.baseline_p90?.request_latency_ms)} | ${value(row.baseline?.throughput_tps)} |`,
                    `| ${value(row.rate)} | ${escapeCell(guideName)} | ${value(row.guide?.ttft_ms)} (${ratioPercent(row.ratio?.ttft_ms)}) | ${value(row.guide_p90?.ttft_ms)} (${ratioPercent(row.ratio_p90?.ttft_ms)}) | ${value(row.guide?.request_latency_ms)} (${ratioPercent(row.ratio?.request_latency_ms)}) | ${value(row.guide_p90?.request_latency_ms)} (${ratioPercent(row.ratio_p90?.request_latency_ms)}) | ${value(row.guide?.throughput_tps)} (${ratioPercent(row.ratio?.throughput_tps)}) |`,
                ]),
            );
        }
        const matrix = comparison.matrix_statistics?.rows || [];
        if (matrix.length) {
            lines.push(
                "", `## ISL × OSL × concurrency matrix${comparisons.length > 1 ? ` — ${comparison.guide || `comparison ${index + 1}`}` : ""}`, "",
                "| ISL | OSL | Concurrency | Throughput | TTFT | TPOT | End-to-end |",
                "|---:|---:|---:|---:|---:|---:|---:|",
                ...matrix.map((row) => `| ${value(row.isl)} | ${value(row.osl)} | ${value(row.concurrency)} | ${ratioPercent(row.ratio?.throughput_tps)} | ${ratioPercent(row.ratio?.ttft_ms)} | ${ratioPercent(row.ratio?.tpot_ms)} | ${ratioPercent(row.ratio?.request_latency_ms)} |`),
            );
        }
    });

    lines.push("", "## Guide-Specific Mechanism Evidence", "");
    guideIds.forEach((guideId) => {
        const section = GUIDE_REPORT_SECTIONS[guideId];
        if (!section) return;
        const guideCases = cases.filter((item) => (item.configuration || item.spec || {}).guide === guideId);
        const scenarios = [...new Set(guideCases.map((item) => item.scenario_name).filter(Boolean))];
        const configuration = guideCases[0]?.configuration || guideCases[0]?.spec || {};
        const guideMetrics = guideCases.find((item) => item.metrics)?.metrics || {};
        const mechanismMetrics = guideMetrics.mechanism_metrics || {};
        const evidenceContract = guideMetrics.evidence_contract || {};
        const measuredEvidence = Object.entries(evidenceContract).filter(([, item]) => item?.status === "measured" && item.value != null);
        lines.push(
            "", `## ${section.title}`, "",
            `**Causal path:** ${section.architecture}`, "",
            `- **Model:** ${configuration.model || "—"}`,
            `- **Serving topology:** ${configuration.prefill_replicas != null ? `${configuration.prefill_replicas}P×TP${configuration.prefill_tensor_parallel_size} / ${configuration.decode_replicas}D×TP${configuration.decode_tensor_parallel_size}` : `${configuration.decode_replicas || configuration.replicas || "—"} replicas × TP${configuration.decode_tensor_parallel_size || configuration.tensor_parallel_size || "—"}`}`,
            `- **Benchmark phases:** ${scenarios.length ? scenarios.join(", ") : benchmark.shared_prefix ? "shared-prefix rate sweep" : benchmark.matrix?.length ? "ISL/OSL/load matrix" : benchmark.workload || "—"}`,
            "", "### Required mechanism evidence", "",
            ...section.evidence.map((item) => `- ${item}`),
            "", "### Measured mechanism evidence", "",
            ...(measuredEvidence.length ? [
                "| Signal | Value | Source |",
                "|---|---:|---|",
                ...measuredEvidence.map(([key, item]) => `| ${escapeCell(key.replaceAll("_", " "))} | ${escapeCell(evidenceValue(item.value, item.unit))} | ${escapeCell(item.source)} |`),
            ] : ["No normalized mechanism evidence was measured for this Guide run."]),
            "", "### Derived and configured evidence", "",
            ...(Object.keys(mechanismMetrics).length ? Object.entries(mechanismMetrics).map(([key, item]) => `- **${key.replaceAll("_", " ")}:** ${evidenceValue(item)}`) : ["No derived Guide evidence is available."]),
            "", "### Trade-offs and limitations", "",
            section.tradeoffs,
            "",
            "Unavailable mechanism metrics must be reported as unavailable; performance deltas alone do not prove the causal mechanism.",
        );
    });

    if (!comparisons.some((item) => item.rate_stage_statistics?.rows?.length)) {
        lines.push("", "## Performance & Capacity Results", "", "A multi-rate capacity sweep is unavailable. Maximum stable rate and saturation cannot be established from a single load point.");
    }
    const primaryMetrics = completed.find((item) => item.metrics)?.metrics || {};
    lines.push(
        "", "## System / Resource Analysis", "",
        "GPU/XPU, CPU, memory, filesystem, KV-cache, queue, request-rate, and output-token-rate measurements are included only when persisted by benchmark-window monitoring.",
        "", "## Trade-offs", "",
        "Mechanism gains are evaluated together with latency, load balance, resource cost, and reliability. Missing evidence is never interpreted as zero cost.",
        "", "## Reliability & Errors", "",
        `${failed.length} case(s) failed or were cancelled. Success and error rates are reported per arm when available.`,
        "", "### Routing & Cache Analysis", "",
        `Prefix cache hit rate: ${value(primaryMetrics.prefix_cache_hit_rate)}. Prefix-group and spillover evidence are reported as unavailable when not present in the immutable result.`,
        "", "### Load Balance Analysis", "",
        `Token load CV: ${value(primaryMetrics.token_load_cv)}. Measured per-pod load evidence is included in the interactive report when available.`,
        "", "## Diagnosis & Recommended Actions", "",
        failed.length ? `${failed.length} case(s) failed or were cancelled. Inspect deployment and benchmark logs before interpreting performance deltas.` : "No failed benchmark cases were recorded. Review routing, queue, and resource evidence before promoting the result.",
        "", "## Benchmark Validity & Limitations", "",
        workflow.status === "succeeded" && completed.length ? "The workflow completed with benchmark metrics. Missing mechanism metrics remain a limitation and are not inferred from performance deltas." : "The workflow is incomplete or has no completed benchmark cases; conclusions require review.",
        "", "## Conclusion & Recommendation", "",
        workflow.status === "succeeded" && completed.length ? "The result is eligible for review; promote only when comparison parity and guide-specific mechanism evidence are complete." : "Do not promote this result until the workflow and required evidence are complete.",
        "", "## Reproducibility Artifacts", "",
        "Includes resolved task and deployment configuration, Router and workload configuration, comparison-arm identity, raw and normalized result references, metric snapshots, versions/commits, model revision, hashes, run IDs, and benchmark timestamps when persisted.",
        "", "### Resolved Deployment / Router / Benchmark Config", "",
        "```json", JSON.stringify(cases.map((item) => ({
            id: item.id, evaluation_run_id: item.evaluation_run_id, deployment_run_id: item.deployment_run_id,
            started_at: item.started_at, finished_at: item.finished_at, kind: item.kind,
            baseline_type: item.baseline_type, configuration: item.configuration || item.spec,
            benchmark: item.benchmark || item.spec?.benchmark, metrics: item.metrics,
            rate_stage_results: item.rate_stage_results, matrix_results: item.matrix_results,
        })), null, 2), "```", "",
    );
    return lines.join("\n");
}
