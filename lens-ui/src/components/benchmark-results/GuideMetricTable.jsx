import { metricDirection as direction, metricColumns as columns } from './metricMetadata.js';
import { caseColor, metricNumber, shortCase } from './linkedExperiment.js';
import { readResultMetric } from './resultExplorer.js';
import { overviewSlo } from './experimentOverview.js';
import { percentChange } from './measurementComparison.js';

function MetricCell({run, metricKey, stat, baseline, onInspect}) {
    const metric = readResultMetric(run, metricKey, stat);
    const before = baseline && readResultMetric(baseline, metricKey, stat);
    const change = before?.quality === metric.quality ? percentChange(metric.value, before.value) : null;
    const favorable = change != null && direction[metricKey] && change * direction[metricKey] > 0;
    return <td className="px-4 py-3 text-right tabular-nums">
        <button type="button" aria-label={`${metric.label} · ${run.id}`} title={metric.reason || metric.source} onClick={()=>onInspect?.(metric)} className="text-sm text-slate-100 underline decoration-slate-700 underline-offset-4 hover:text-cyan-200">{metricNumber(metric.value)}</button>
        {metric.value == null && <span className="block text-[10px] text-slate-500">Not collected</span>}
        {metric.quality==='estimated' && <span className="block text-[10px] text-amber-300">Estimate</span>}
        {change != null && <span title="Change vs. baseline at the same load and input/output lengths" className={`block text-[10px] ${direction[metricKey] && change !== 0 ? favorable?'text-emerald-300':'text-rose-300':'text-slate-500'}`}>{change>0?'+':''}{metricNumber(change)}%</span>}
    </td>;
}

export default function GuideMetricTable({details, points, profile, stat, selected, onSelect, onInspect}) {
    const groups = new Map();
    for (const run of points) {
        const key = JSON.stringify([run.loadKind, run.isl, run.osl]);
        if (!groups.has(key)) groups.set(key, {key, loadKind:run.loadKind, isl:run.isl, osl:run.osl, runs:[]});
        groups.get(key).runs.push(run);
    }
    const baselineFor = run => {
        if(run.kind==='baseline') return null;
        const matches = points.filter(point=>point.kind==='baseline' && ['loadKind','load','isl','osl'].every(key=>run[key]!=null && point[key]===run[key]));
        return matches.length===1 ? matches[0] : null;
    };
    const observationLabel = (run, group) => {
        const repeats = group.runs.filter(point=>point.caseId===run.caseId && point.load===run.load);
        return repeats.length>1 ? `Observation ${repeats.findIndex(point=>point.id===run.id)+1}` : null;
    };
    return <div className="mt-4 space-y-5">{[...groups.values()].map(group=><div key={group.key} className="overflow-x-auto rounded-lg border border-slate-800">
        <table aria-label={`${group.loadKind==='rate'?'Request rate':'Concurrency'} performance · ${group.isl??'unknown'} / ${group.osl??'unknown'} tokens`} className="w-full min-w-[720px] text-left text-xs">
            <caption className="bg-slate-950/30 px-4 py-3 text-left text-xs text-slate-400">Input / output tokens: <span className="text-slate-200">{metricNumber(group.isl)} / {metricNumber(group.osl)}</span> · {group.loadKind==='rate'?'Request rate (req/s)':group.loadKind==='concurrency'?'Concurrency (requests)':'Full-run results'}</caption>
            <thead className="bg-slate-950/50 text-slate-400"><tr><th scope="col" className="px-4 py-3 font-medium">Configuration</th><th scope="col" className="px-4 py-3 font-medium">{group.loadKind==='rate'?'Req/s':group.loadKind==='concurrency'?'Concurrency':'Scope'}</th>{profile.primary.map(key=>{
                const [label,unit] = columns[key] || [key.toUpperCase(),`${stat.toUpperCase()} · ms`];
                return <th key={key} scope="col" className="px-4 py-3 text-right font-medium"><span className="block text-slate-300">{label}</span><span className="mt-1 block text-[10px] text-slate-500">{unit}</span></th>;
            })}<th scope="col" className="px-4 py-3 font-medium">SLO status</th></tr></thead>
            <tbody className="divide-y divide-slate-800">{[...group.runs].sort((a,b)=>(a.load??0)-(b.load??0)).map(run=><tr key={run.id} className={selected?.id===run.id?'bg-cyan-950/25':'hover:bg-slate-800/30'}>
                <th scope="row" className="max-w-64 px-4 py-3 font-normal text-slate-300"><span className="mr-2 inline-block h-2 w-2 rounded-full" style={{background:caseColor(points,run.caseId)}}/>{shortCase(run)}{observationLabel(run,group) && <span className="mt-1 block text-[10px] text-slate-400">{observationLabel(run,group)}</span>}{run.kind==='baseline' && <span className="mt-1 block text-[10px] text-slate-500">Baseline</span>}</th>
                <td className="px-4 py-3"><button type="button" aria-label={`Inspect load point ${run.id}`} aria-pressed={selected?.id===run.id} onClick={()=>onSelect?.(run.id)} className="rounded border border-slate-700 px-3 py-1.5 text-cyan-200 hover:border-cyan-500">{run.load==null?'Full run':metricNumber(run.load)}</button></td>
                {profile.primary.map(key=><MetricCell key={key} run={run} metricKey={key} stat={stat} baseline={baselineFor(run)} onInspect={onInspect}/>) }<td className={`px-4 py-3 ${overviewSlo(run,details)==='Not met'?'text-rose-300':overviewSlo(run,details)==='Met'?'text-emerald-300':'text-slate-500'}`}>{overviewSlo(run,details)}</td></tr>)}</tbody>
        </table>
    </div>)}
    </div>;
}
