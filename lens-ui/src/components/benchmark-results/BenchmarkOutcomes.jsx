import { Select } from '../ui/FormControls';
import { useState } from 'react';
import { readResultMetric } from './resultExplorer.js';
import { matchingRuns, metricNumber, shortCase } from './linkedExperiment.js';

export default function BenchmarkOutcomes({ runs, selected }) {
    const [referenceId, setReferenceId] = useState('');
    const knownConditions = selected?.load != null && selected?.isl != null && selected?.osl != null;
    const peers = knownConditions ? matchingRuns(runs, selected).filter(run => run.caseId !== selected?.caseId) : [];
    const reference = peers.find(run => run.id === referenceId) || peers[0];
    if (!selected) return null;
    return <section aria-label="Benchmark outcomes" className="border-y border-slate-800/70 py-5">
        <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-sm font-medium text-slate-200">Selected result</h3><p className="mt-1 text-xs text-slate-400">{shortCase(selected)} · {selected.load == null ? 'Full run' : `${selected.load} ${selected.loadKind === 'rate' ? 'req/s' : 'concurrent'}`} · Input {selected.isl ?? '—'} / Output {selected.osl ?? '—'}</p></div>{reference && <label className="text-xs text-slate-400">Reference <Select aria-label="Outcome reference" value={reference.id} onChange={e => setReferenceId(e.target.value)} className="ml-2 w-auto max-w-full text-xs">{peers.map(run => <option key={run.id} value={run.id}>{shortCase(run)}</option>)}</Select></label>}</div>
        <div className="mt-5 grid gap-5 sm:grid-cols-3">{[['throughput', 'Output throughput', false], ['ttft', 'Time to first token · P95', true], ['tpot', 'Time per output token · P95', true]].map(([key, label, lowerBetter]) => {
            const metric = readResultMetric(selected, key, 'p95');
            const before = reference ? readResultMetric(reference, key, 'p95').value : null;
            const change = before > 0 && metric.value != null ? (metric.value - before) / before * 100 : null;
            const favorable = change != null && (lowerBetter ? change < 0 : change > 0);
            return <div key={key}><p className="text-xs text-slate-400">{label}</p><p className="mt-2 text-2xl font-medium tabular-nums text-slate-100">{metricNumber(metric.value)} <span className="text-xs font-normal text-slate-500">{metric.value != null ? metric.unit : 'Not recorded'}</span></p>{reference && <p className={`mt-1 text-sm ${change == null || change === 0 ? 'text-slate-500' : favorable ? 'text-emerald-400' : 'text-amber-300'}`}>{change == null ? 'Change unavailable' : `${change > 0 ? '+' : ''}${metricNumber(change)}%`} <span className="text-xs text-slate-500">{before != null ? `vs. ${metricNumber(before)} ${metric.unit}` : 'Reference not recorded'}</span></p>}</div>;
        })}</div>
    </section>;
}
