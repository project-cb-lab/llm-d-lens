import assert from "node:assert";
import { buildEvaluationMarkdown, evaluationReportFilename } from "./evaluationReport.js";

const details = {
    workflow: { id: "run-1", name: "Qwen / Routing", status: "succeeded", created_at: "2026-09-02T00:00:00Z" },
    cases: [
        { case: { id: "guide", kind: "guide", status: "succeeded", configuration: { guide: "precise-prefix-cache-routing", model: "Qwen/Qwen3-32B" }, benchmark: { harness: "inference-perf", workload: "shared-prefix" } } },
        { case: { id: "base", kind: "baseline", baseline_type: "kubernetes-service", status: "succeeded", configuration: { model: "Qwen/Qwen3-32B" } } },
    ],
    report: {
        generated_at: "2026-09-02T01:00:00Z",
        comparisons: [{
            guide: "precise-prefix-cache-routing", baseline_type: "kubernetes-service",
            guide_configuration: { guide: "tiered-prefix-cache", guide_variant: "native/cpu/base", model: "Qwen/Qwen3-32B" },
            baseline_configuration: { guide: "tiered-prefix-cache", guide_variant: "base", model: "Qwen/Qwen3-32B" },
            guide_metrics: { throughput_tps: 14892, ttft_ms: 190 },
            baseline_metrics: { throughput_tps: 6986, ttft_ms: 54600 },
            delta_percent: { throughput_tps: 113.2, ttft_ms: -99.7 },
            rate_stage_statistics: { rows: [{ rate: 60, duration: 60, guide: { throughput_tps: 14892, ttft_ms: 190 }, baseline: { throughput_tps: 6986, ttft_ms: 54600 }, guide_p90: { ttft_ms: 300 }, baseline_p90: { ttft_ms: 60000 }, ratio: { throughput_tps: 2.132, ttft_ms: 0.0035 }, ratio_p90: { ttft_ms: 0.005 } }] },
        }],
    },
};

const markdown = buildEvaluationMarkdown(details, "https://prism.example/report?evaluationId=run-1");
assert.match(markdown, /^# Qwen \/ Routing/m);
assert.match(markdown, /## Comparison summary/);
assert.match(markdown, /14,892 vs 6,986 \(\+113\.2%\)/);
assert.match(markdown, /## Shared-prefix rate ladder/);
assert.match(markdown, /VRAM \+ CPU RAM · Qwen3-32B.*VRAM-only · Qwen3-32B/);
assert.match(markdown, /190 \(-99\.7%\)/);
assert.match(markdown, /300 \(-99\.5%\)/);
assert.match(markdown, /Interactive report.*https:\/\/prism\.example/);
assert.match(markdown, /## Precise Prefix Cache routing evidence/);
assert.match(markdown, /KV events.*precise per-pod block index/);
assert.match(markdown, /Precise versus approximate routing/);
for (const section of [
    "Executive Summary", "Test Environment & Software Versions", "Experiment Configuration & Comparison Arms",
    "Benchmark Methodology & Workload", "Deployment & Functional Validation", "Performance & Capacity Results",
    "Guide-Specific Mechanism Evidence", "System / Resource Analysis", "Trade-offs", "Reliability & Errors",
    "Diagnosis & Recommended Actions", "Benchmark Validity & Limitations", "Conclusion & Recommendation",
    "Reproducibility Artifacts",
]) assert.match(markdown, new RegExp(`## ${section.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
assert.strictEqual(evaluationReportFilename(details), "qwen-routing-benchmark-report.md");

console.log("evaluationReport tests passed");
