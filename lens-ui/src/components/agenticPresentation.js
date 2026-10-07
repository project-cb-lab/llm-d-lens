import { formatTimestamp } from '../utils/formatTimestamp.js';

export function historicalEvidenceSummary(samples) {
    const history = Array.isArray(samples) ? samples : [];
    if (!history.length) return null;
    const failed = history.filter((sample) => sample.outcome === 'failed').length;
    return {
        sampleCount: history.length,
        successfulCount: history.length - failed,
        failedCount: failed,
    };
}

export function generatorLabel(generator) {
    return generator === 'ai-mcp' ? 'AI MCP generator' : 'Deterministic generator';
}

export function relaxedAicCandidates(run) {
    const trace = run?.generator_tool_trace?.find((entry) => entry.tool === 'search_aic_with_relaxation' && entry.status === 'success');
    return Array.isArray(trace?.result?.candidates) ? trace.result.candidates : [];
}

export function historicalBenchmarkLabel(sample) {
    const observedAt = formatTimestamp(sample.observed_at, { invalid: () => 'unknown date' });
    const relevance = Number.isFinite(sample.match_confidence)
        ? `${Math.round(sample.match_confidence * 100)}% relevance`
        : 'unknown relevance';
    const metrics = [
        sample.ttft_p95_ms !== null && sample.ttft_p95_ms !== undefined ? `TTFT ${sample.ttft_p95_ms} ms` : null,
        sample.tpot_p95_ms !== null && sample.tpot_p95_ms !== undefined ? `TPOT ${sample.tpot_p95_ms} ms` : null,
        sample.throughput_tokens_per_s !== null && sample.throughput_tokens_per_s !== undefined
            ? `${sample.throughput_tokens_per_s} tokens/s` : null,
        sample.failure_reason ? `failure: ${sample.failure_reason}` : null,
    ].filter(Boolean).join(', ');
    return `${sample.benchmark_id} | ${observedAt} | ${sample.outcome} | ${relevance}${metrics ? ` | ${metrics}` : ''}`;
}