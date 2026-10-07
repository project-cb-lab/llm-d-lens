import { Fragment, useState } from 'react';
import { experimentHierarchy, experimentPoints, caseColor, metricNumber, shortCase } from './linkedExperiment.js';
import { readResultMetric } from './resultExplorer.js';
import { overviewSlo } from './experimentOverview.js';
import { LATENCY_AXES } from './frontierData.js';
import { measurementBaseline, percentChange } from './measurementComparison.js';

const metrics = [['throughput', 'Output throughput'], ['ttft', 'TTFT'], ['ntpot', 'NTPOT'], ['itl', 'ITL'], ['tpot', 'TPOT'], ['e2e', 'End-to-end'], ['successRate', 'Success rate']];
const control = 'rounded-md border border-slate-700/70 bg-[#0b1422] px-2.5 py-1.5 text-[11px] text-slate-300 outline-none focus:border-cyan-400';

function MetricValue({run, baseline, metric, stat}) {
    const value = readResultMetric(run, metric, stat).value;
    const change = baseline && baseline.caseId !== run.caseId ? percentChange(value, readResultMetric(baseline, metric, stat).value) : null;
    const better = metric === 'throughput' || metric === 'successRate' ? change > 0 : change < 0;
    return <><span>{metricNumber(value)}</span><span className={`mt-1 block text-[10px] ${change == null || change === 0 ? 'text-slate-500' : better ? 'text-emerald-300' : 'text-rose-300'}`}>{baseline?.caseId === run.caseId ? 'Baseline' : change == null ? '—' : `${change > 0 ? '+' : ''}${metricNumber(change)}%`}</span></>;
}

export default function ExperimentMap({runs, details, metric: sharedMetric, stat: sharedStat, selected, onSelect}) {
    const [localMetric, setMetric] = useState('ttft');
    const [localStat, setStat] = useState('p95');
    const [baselineId, setBaselineId] = useState('');
    const [localSelected, setSelected] = useState(null);
    const metric = sharedMetric ?? localMetric, stat = sharedStat ?? localStat;
    const hierarchy = experimentHierarchy(runs), points = experimentPoints(runs);
    const baselineCase = hierarchy.some(group => group.id === baselineId) ? baselineId : hierarchy[0]?.id;
    const active = selected?.id ?? localSelected;
    const latency = LATENCY_AXES.some(([key]) => key === metric);
    const metricLabel = `${metrics.find(([key]) => key === metric)[1]}${latency ? ` ${stat.toUpperCase()} (ms)` : metric === 'successRate' ? ' (%)' : ' (tok/s)'}`;
    return <section aria-label="Experiment measurements" className="space-y-4 border-t border-slate-800 pt-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
            <div><h3 className="text-sm font-semibold text-slate-200">Results by configuration and load</h3><p className="mt-1 text-xs text-slate-400">Each row is one measured configuration, load and input/output length combination. Changes compare matching baseline results. Select a row for all latency percentiles.</p></div>
            <div className="flex flex-wrap items-center gap-2">
                {sharedMetric == null && <><select aria-label="Measurement metric" className={control} value={metric} onChange={event => setMetric(event.target.value)}>{metrics.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><select aria-label="Measurement statistic" className={control} value={stat} disabled={!latency} onChange={event => setStat(event.target.value)}>{['mean', 'p50', 'p90', 'p95', 'p99'].map(value => <option key={value} value={value}>{value.toUpperCase()}</option>)}</select></>}
                <label className="flex items-center gap-2 text-[11px] text-slate-400">Baseline<select aria-label="Measurement baseline" className={control} value={baselineCase} onChange={event => setBaselineId(event.target.value)}>{hierarchy.map((group,index) => <option key={group.id} value={group.id}>C{index+1} · {shortCase(group.loads[0].runs[0])}</option>)}</select></label>
            </div>
        </div>
        <div className="overflow-x-auto rounded-xl border border-slate-800"><table className="w-full min-w-[750px] text-left text-xs"><thead className="bg-slate-900 text-[10px] text-slate-400"><tr>{['Configuration', 'Load', 'Input / output tokens', 'Output tok/s', ...(metric === 'throughput' ? [] : [metricLabel]), 'SLO status'].map(label => <th key={label} scope="col" className="px-4 py-3 font-medium">{label}</th>)}</tr></thead><tbody className="divide-y divide-slate-800/60">{hierarchy.flatMap((group,index) => group.loads.flatMap(load => load.runs.map(run => {
            const slo = overviewSlo(run,details), baseline = measurementBaseline(points, run, baselineCase);
            return <Fragment key={run.id}><tr className={active === run.id ? 'bg-cyan-950/25' : 'hover:bg-slate-800/30'}>
                <th scope="row" className="px-4 py-3 font-normal"><button type="button" aria-expanded={active === run.id} aria-label={`Inspect measurement ${run.id}`} onClick={() => onSelect ? onSelect(run.id) : setSelected(active === run.id ? null : run.id)} className="text-left underline decoration-slate-600 underline-offset-4 hover:text-cyan-200"><span style={{color:caseColor(runs,group.id)}}>C{index+1}</span><span className="ml-2 text-slate-300">{shortCase(run)}</span></button></th>
                <td className="px-4 py-3 text-slate-300">{run.load ?? 'Full run'} {run.load == null ? '' : run.loadKind==='rate'?'req/s':'concurrent'}</td>
                <td className="px-4 py-3 tabular-nums text-slate-400">{run.isl??'—'} / {run.osl??'—'}</td>
                <td className="px-4 py-3 tabular-nums text-slate-300"><MetricValue run={run} baseline={baseline} metric="throughput"/></td>
                {metric !== 'throughput' && <td className="px-4 py-3 tabular-nums text-slate-300"><MetricValue run={run} baseline={baseline} metric={metric} stat={stat}/></td>}
                <td className={`px-4 py-3 ${slo==='Not met'?'text-rose-300':slo==='Met'?'text-emerald-300':'text-slate-500'}`}>{slo}</td>
            </tr>{active === run.id && <tr><td colSpan={metric === 'throughput' ? 5 : 6} className="bg-slate-950/40 px-4 py-4"><table aria-label="Measurement latency percentiles" className="w-full text-left text-[11px]"><caption className="mb-3 text-left text-xs text-slate-300">Latency percentiles · ms</caption><thead><tr>{['Metric', 'Mean', 'P50', 'P90', 'P95', 'P99'].map(label => <th scope="col" className="pb-2 font-medium text-slate-400" key={label}>{label}</th>)}</tr></thead><tbody>{LATENCY_AXES.map(([key,label]) => <tr key={key} className="border-t border-slate-800/50"><th scope="row" className="py-2 font-normal text-slate-400">{label}</th>{['mean','p50','p90','p95','p99'].map(statistic => <td className="py-2 tabular-nums text-slate-300" key={statistic}><MetricValue run={run} baseline={baseline} metric={key} stat={statistic}/></td>)}</tr>)}</tbody></table></td></tr>}</Fragment>;
        })))}</tbody></table></div>
        <p className="text-[10px] text-slate-400">SLO status checks configured performance targets: Met = all targets passed; Not met = at least one failed; Not measured = required data is missing; Measure only = no targets configured.</p>
        <p className="text-[10px] text-slate-500">— No unique matching baseline, missing value, or zero baseline. Green: higher throughput / success or lower latency; red: the reverse.</p>
    </section>;
}
