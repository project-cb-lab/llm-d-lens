export const SUPPORTED_BENCHMARK_GUIDES = [
    "optimized-baseline",
    "pd-disaggregation",
    "precise-prefix-cache-routing",
    "tiered-prefix-cache",
];

const commonDiagnosis = [
    "Performance Bottleneck", "Saturation", "Queue Growth", "Resource Bottleneck",
    "Timeout / Failure", "Per-Pod Abnormality", "Metrics Missing",
    "Benchmark Parity Problem", "Benchmark Validity Problem", "Calibration Problem",
];

export const GUIDE_RESULT_PROFILES = {
    "optimized-baseline": {
        label: "Optimized Baseline",
        primaryMetrics: ["Prefix Cache Hit Rate", "Token Load CV"],
        arms: ["Kubernetes RR", "Load-only", "Affinity Policy Only", "Full Optimized Baseline"],
        benefits: ["Total Routing Benefit", "Prefix Affinity Benefit", "Token Load Ranking Benefit"],
        mechanismQuestion: "Did approximate prefix locality improve cache reuse without creating unacceptable load imbalance?",
        mechanism: ["Prefix Cache Hit Rate", "Prefix Group Stickiness", "Prefix Group × Pod Heatmap", "Request Destination Distribution", "Per-Pod In-flight Token Load", "Token Load CV", "Cache Hit Rate vs Load CV", "Locality–Balance Pareto", "Affinity Relaxation / Spillover", "Recomputed Prefill Tokens", "Avoided Prefill Ratio"],
        system: ["Per-Pod Requests/s", "Per-Pod Output Tokens/s", "In-flight Tokens", "Running / Waiting Requests", "Load CV", "Cache Locality vs Load Balance", "Saturation Behavior", "Affinity Relaxation", "Router Overhead", "Accelerator Utilization", "Resource Efficiency"],
        deployment: ["Routing plugins", "Prefix Affinity configuration", "Token Load configuration", "Calibration status"],
        diagnosis: [...commonDiagnosis, "Low Cache Locality", "Sticky Endpoint Overload", "Poor Token Load Balance", "Affinity Relaxation Too Early / Too Late", "Router / EPP Bottleneck"],
        reportSection: "Routing Locality & Load Balance",
    },
    "pd-disaggregation": {
        label: "P/D Disaggregation",
        primaryMetrics: ["ITL / TPOT P95/P99", "Decode Interference Penalty", "KV Transfer Success Rate"],
        arms: ["Aggregated Baseline", "Symmetric P/D", "Tuned Heterogeneous P/D"],
        benefits: ["Phase Separation Benefit", "Topology / Specialization Benefit", "Total P/D Benefit"],
        mechanismQuestion: "Did Prefill and Decode actually separate, did KV transfer work, and did Decode remain stable during Prefill pressure?",
        mechanism: ["P/D Decision Ratio", "Prefill Endpoint Distribution", "Decode Endpoint Distribution", "Prefill Burst Timeline", "Decode ITL before / during / after Prefill Burst", "Decode Interference Penalty", "KV Transfer Success / Failure", "KV Transfer Timeout / Errors", "Transfer Latency P50/P95/P99", "Transfer Bandwidth", "RDMA / TCP / UCX path", "KV Peer Connectivity", "KV Handoff Status"],
        system: ["Prefill Accelerator Utilization", "Prefill Running Requests", "Prefill Queue", "Prefill Pressure", "Decode Accelerator Utilization", "Decode Active Requests", "Decode KV Utilization", "Decode Pressure", "ITL / TPOT", "P:D Balance Gap", "Prefill-bound / Decode-bound Indicator", "P:D Ratio Sweep", "Prefill GPU Share vs TTFT", "Prefill GPU Share vs ITL", "Prefill GPU Share vs SLO Goodput", "TTFT vs ITL Pareto Frontier", "Recommended P:D Topology", "ISL × OSL Workload Geometry Heatmap"],
        deployment: ["Prefill Pool", "Decode Pool", "P:D topology", "KV Transfer Backend", "RDMA / UCX / TCP Status", "Transfer Validation", "Calibration"],
        diagnosis: [...commonDiagnosis, "Prefill-bound", "Decode-bound", "Transfer-bound", "Network / RDMA Bottleneck", "P:D Ratio Imbalance", "Prefill Interference Not Isolated", "Decode KV Pressure"],
        reportSection: "P/D Isolation & KV Transfer",
    },
    "precise-prefix-cache-routing": {
        label: "Precise Prefix Cache Routing",
        primaryMetrics: ["Effective Cached Prompt Fraction", "Prefill Recomputation Avoided", "Precise Index Health"],
        arms: ["Round Robin", "Approx Prefix", "Precise Prefix"],
        benefits: ["Prefix-aware Routing Benefit", "Precise KV-state Benefit", "Total Precise Routing Benefit"],
        mechanismQuestion: "Did real KV-state knowledge route requests to genuinely cache-warm endpoints and reduce Prefill recomputation compared with Approx routing?",
        mechanism: ["KV Event Publisher Coverage", "KV Event Subscriber Coverage", "KV Event Rate", "Active Subscribers", "KV Event Errors", "Index Admissions", "Index Evictions", "Index Lookups", "Index Entry Count", "Matched Blocks / Lookup", "Index Lookup Latency", "Cache-Warm Route Share", "Prefix Group Stickiness", "Prefix Group × Pod Heatmap", "Cached Prompt Fraction", "Recomputed Prompt Tokens", "Precise Recompute Savings vs Approx", "Precise Recompute Savings vs RR", "Affinity / Load-gate Behavior"],
        system: ["Request Assignments", "Prompt Tokens/s", "Cached Tokens/s", "Recomputed Tokens/s", "Output Tokens/s", "Queue", "In-flight Tokens", "KV Utilization", "Accelerator Utilization", "Render / Tokenizer CPU Usage", "Cache Eviction Activity", "Pod Restart", "Cold Pod / Scale-out behavior", "Subscriber Recovery", "Index Recovery Time", "Replay Status", "Stale Locality Decisions", "Requests Affected", "TTFT / Cache Locality During Recovery", "EPP Scheduler Latency", "Index Lookup Latency", "Render / Tokenization Latency", "Plugin Latency"],
        deployment: ["KV Event Publishers", "Precise Index Status", "Block Size", "ZMQ Event Port", "Replay Status", "Render / Tokenizer Status", "Calibration"],
        diagnosis: [...commonDiagnosis, "Precise Index Unhealthy", "Missing Publisher / Subscriber", "Block Size Mismatch", "Low Cache-Warm Routing Share", "High Prefill Recomputation", "Precise Not Better Than Approx", "Stale Index / Recovery Failure", "Render / Tokenizer Bottleneck"],
        reportSection: "Precise KV-State Evidence",
    },
    "tiered-prefix-cache": {
        label: "Tiered Prefix Cache",
        primaryMetrics: ["Effective Prefix Hit Rate", "Effective Cache Capacity", "Working Set / HBM Capacity"],
        arms: ["HBM-only Baseline", "Tiered Cache Candidate"],
        benefits: ["Tiered Cache Benefit"],
        mechanismQuestion: "Did the working set exceed HBM, did lower tiers retain evicted KV, and did this reduce cache misses / recomputation?",
        mechanism: ["Working Set", "HBM Capacity", "CPU Cache Capacity", "Filesystem Capacity", "Effective Cache Capacity", "Working Set / HBM Ratio", "Working Set / Effective Capacity Ratio", "HBM Hit Rate", "Lower Tier Hit Rate", "CPU Hit Rate", "FS Hit Rate", "Miss Rate", "Effective Prefix Hit Rate", "HBM Evictions", "Offloaded Bytes", "Restored Bytes", "Offload Bandwidth", "Restore Bandwidth", "HBM Cache Utilization", "CPU Cache Utilization", "FS Cache Utilization", "Working Set vs Cache Capacity", "HBM / CPU / FS / Miss stacked bar", "Offload / Restore Timeline", "Cache Utilization Timeline"],
        system: ["Accelerator Utilization", "HBM Usage", "CPU Memory Usage", "CPU Cache Usage", "CPU Utilization", "Transfer Bandwidth", "Filesystem Read / Write / Latency", "Queue Depth", "Capacity Gain", "Effective Prefix Hit Gain", "TTFT Improvement", "Stable Rate Improvement", "TPOT Delta", "Restore Traffic", "CPU Memory Cost", "Filesystem Cost", "Evidence-backed Verdict"],
        deployment: ["Cache Topology", "Connector Backend", "CPU Cache Size", "Filesystem / PVC Status", "Tiered Cache Validation"],
        diagnosis: [...commonDiagnosis, "Working Set Does Not Exceed HBM", "CPU Offload Not Observed", "Insufficient Cache Pressure", "HBM Saturation", "CPU Cache Saturation", "Restore / Transfer Bottleneck", "Filesystem Bottleneck", "Low Effective Cache Hit", "CPU Memory Pressure"],
        reportSection: "Tiered Cache Evidence",
    },
};

export function guideResultProfile(guideType) {
    return GUIDE_RESULT_PROFILES[guideType] || GUIDE_RESULT_PROFILES["optimized-baseline"];
}
