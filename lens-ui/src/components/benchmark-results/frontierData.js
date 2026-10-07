import { experimentPoints } from './linkedExperiment.js';
import { readResultMetric } from './resultExplorer.js';

export const LATENCY_AXES = [['ntpot', 'NTPOT', 'Normalized TPOT'], ['tpot', 'TPOT', 'TPOT'], ['ttft', 'TTFT', 'TTFT'], ['itl', 'ITL', 'ITL'], ['e2e', 'E2E Latency', 'E2E Latency']];
export const OUTPUT_AXES = [['throughput', 'Output', 'Output Tokens/sec'], ['inputThroughput', 'Input', 'Input Tokens/sec'], ['total', 'Total', 'Total Tokens/sec'], ['requestRate', 'QPS', 'Requests/sec']];
export const PERCENTILES = ['p50', 'p90', 'p95', 'p99'];

export function allocatedChips(run) {
    const c = run.configuration || {}, d = c.decode || c.serving || {};
    const replicas = d.replicaCount ?? d.replicas ?? c.decode_replicas ?? c.replicas;
    const tp = d.tensorParallelSize ?? d.tensor_parallel_size ?? c.decode_tensor_parallel_size ?? c.tensor_parallel_size;
    const pp = d.pipelineParallelSize ?? 1;
    const positive = value => value != null && value !== '' && Number.isFinite(Number(value)) && Number(value) > 0;
    if (![replicas, tp, pp].every(positive)) return null;
    let count = Number(replicas) * Number(tp) * Number(pp);
    const prefill = c.prefill;
    if (prefill || c.prefill_replicas != null) {
        const pr = prefill?.replicaCount ?? prefill?.replicas ?? c.prefill_replicas;
        if (Number(pr) !== 0) {
            const pt = prefill?.tensorParallelSize ?? prefill?.tensor_parallel_size ?? c.prefill_tensor_parallel_size;
            const ppp = prefill?.pipelineParallelSize ?? 1;
            if (![pr, pt, ppp].every(positive)) return null;
            count += Number(pr) * Number(pt) * Number(ppp);
        }
    }
    return count;
}

/** Preserve exact recorded percentiles; never fill missing data with another statistic. */
export function frontierPoints(runs, { latency, stats, output, perChip = false, logScale = false, cap = null }) {
    return experimentPoints(runs).flatMap(run => {
        let y;
        if (output === 'total') {
            const input = readResultMetric(run, 'inputThroughput').value;
            const generated = readResultMetric(run, 'throughput').value;
            y = input != null && generated != null ? input + generated : null;
        } else y = readResultMetric(run, output).value;
        if (perChip) {
            const chips = allocatedChips(run);
            y = chips && y != null ? y / chips : null;
        }
        if (!Number.isFinite(y)) return [];
        return stats.flatMap(stat => {
            const x = readResultMetric(run, latency, stat).value;
            return Number.isFinite(x) && (!logScale || x > 0) && (cap == null || x <= cap) ? [{run, stat, x, y}] : [];
        });
    });
}
