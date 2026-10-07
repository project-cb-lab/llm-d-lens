import { useId, useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { CartesianGrid, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from 'recharts';
import { ChartTooltip, ChartTooltipRow } from '../ui/charts/ChartTooltip.jsx';
import { caseColor, experimentPoints, metricNumber, shortCase } from './linkedExperiment.js';
import { frontierPoints, LATENCY_AXES, OUTPUT_AXES, PERCENTILES } from './frontierData.js';

const button = 'rounded-md px-2.5 py-1.5 text-[10px] font-semibold transition focus-visible:outline-2 focus-visible:outline-cyan-300';
function Options({ label, values, selected, onChange }) {
    return <div role="group" aria-label={label} className="flex flex-wrap items-center gap-1 rounded-lg border border-slate-700/60 bg-slate-950/40 p-0.5">{values.map(([key, name]) => <button type="button" key={key} aria-pressed={selected === key} onClick={() => onChange(key)} className={`${button} ${selected === key ? 'bg-slate-600 text-white' : 'text-slate-400 hover:text-white'}`}>{name}</button>)}</div>;
}
const statShape = (stat, cx, cy, size, props) => stat === 'p90'
    ? <rect x={cx-size} y={cy-size} width={size*2} height={size*2} rx={1} {...props}/>
    : stat === 'p99' ? <path d={`M${cx},${cy-size-2} L${cx+size+2},${cy} L${cx},${cy+size+2} L${cx-size-2},${cy} Z`} {...props}/>
    : stat === 'p95' ? <path d={`M${cx},${cy-size-2} L${cx+size+1},${cy+size} L${cx-size-1},${cy+size} Z`} {...props}/>
    : <circle cx={cx} cy={cy} r={size} {...props}/>;

export default function BenchmarkFrontier({ runs, selected, onSelect, initialLatency = 'ttft' }) {
    const filterId = useId();
    const [latency, setLatency] = useState(initialLatency);
    const [output, setOutput] = useState('throughput');
    const [filtersOpen, setFiltersOpen] = useState(true);
    const [logScale, setLogScale] = useState(false);
    const [perChip, setPerChip] = useState(false);
    const [stats, setStats] = useState(PERCENTILES);
    const [cap, setCap] = useState(null);
    const latencyLabel = LATENCY_AXES.find(([key]) => key === latency)[2];
    const outputLabel = OUTPUT_AXES.find(([key]) => key === output)[2];
    const unit = `${output === 'requestRate' ? 'req/s' : 'tok/s'}${perChip ? '/chip' : ''}`;
    const points = experimentPoints(runs);
    const cases = [...new Set(points.map(run => run.caseId))];
    const settings = {latency, stats, output, perChip, logScale};
    const measured = frontierPoints(runs, settings);
    const data = cap == null ? measured : measured.filter(point => point.x <= cap);
    const maximum = Math.max(1, ...measured.map(point => point.x));
    const groupKey = p => JSON.stringify([p.run.caseId, p.run.loadKind, p.run.isl, p.run.osl, p.stat]);
    const series = [...new Set(data.map(groupKey))].map(key => ({ key, points: data.filter(p => groupKey(p) === key).sort((a, b) => a.run.load - b.run.load) }));
    const toggleStat = stat => setStats(current => current.includes(stat) ? current.length > 1 ? current.filter(value => value !== stat) : current : PERCENTILES.filter(value => value === stat || current.includes(value)));
    return <section aria-label={`Benchmark performance frontier · ${latency.toUpperCase()}`} className="min-w-0 overflow-hidden rounded-xl border border-slate-800 bg-[#0e1628]">
        <div className="flex flex-wrap items-start justify-between gap-3 px-5 py-4"><div><h3 className="text-base font-semibold text-slate-100">{outputLabel} vs {latencyLabel}{perChip ? ' · Per chip' : ''}</h3><p className="mt-2 text-xs text-slate-400">{cases.length} configurations · {points.length} measurements · {stats.map(stat => stat.toUpperCase()).join(' / ')}</p></div><button type="button" aria-expanded={filtersOpen} aria-controls={filterId} onClick={() => setFiltersOpen(value => !value)} className={`${button} inline-flex items-center gap-2 border border-slate-600/60 bg-slate-800 text-slate-300`}>FILTERS {filtersOpen ? <ChevronUp size={13}/> : <ChevronDown size={13}/>}</button></div>
        {filtersOpen && <div id={filterId} className="grid gap-4 border-y border-slate-800 bg-[#131e31] px-5 py-4 lg:grid-cols-[minmax(0,1fr)_auto]">
            <div className="space-y-3">{[['X-AXIS', <Options key="x" label="X axis" values={LATENCY_AXES} selected={latency} onChange={value => {setLatency(value); setCap(null);}}/>], ['Y-AXIS', <Options key="y" label="Y axis" values={OUTPUT_AXES} selected={output} onChange={setOutput}/>]].map(([label, control]) => <div key={label} className="flex flex-wrap items-center gap-3"><span className="w-12 text-[9px] font-bold tracking-wider text-slate-400">{label}:</span>{control}</div>)}</div>
            <div className="space-y-3"><div className="flex flex-wrap items-center gap-3"><div className="flex rounded-lg border border-slate-700/60 bg-slate-950/40 p-0.5"><button type="button" aria-pressed={logScale} onClick={() => setLogScale(value => !value)} className={`${button} ${logScale ? 'bg-amber-600 text-white' : 'text-slate-400'}`}>Log Scale</button><button type="button" aria-pressed={perChip} onClick={() => setPerChip(value => !value)} className={`${button} ${perChip ? 'bg-cyan-700 text-white' : 'text-slate-400'}`}>Per Chip</button></div><div role="group" aria-label="Latency percentiles" className="flex gap-1 rounded-lg border border-slate-700/60 bg-slate-950/40 p-0.5">{PERCENTILES.map(stat => <button type="button" key={stat} aria-pressed={stats.includes(stat)} onClick={() => toggleStat(stat)} className={`${button} ${stats.includes(stat) ? 'bg-violet-600 text-white' : 'text-slate-400'}`}>{stat.toUpperCase()}</button>)}</div></div>
                <div className="flex items-center gap-3 rounded-lg border border-slate-700/60 bg-slate-950/40 px-2 py-1"><span className="text-[9px] font-bold text-slate-400">CAP:</span><button type="button" aria-pressed={cap == null} onClick={() => setCap(null)} className={`${button} ${cap == null ? 'bg-cyan-600 text-white' : 'text-slate-400'}`}>AUTO</button><input aria-label={`${latencyLabel} latency cap`} type="range" min="0.1" step="any" max={Math.max(maximum, cap || 0)} value={cap ?? maximum} onChange={event => setCap(Number(event.target.value))} className="min-w-0 flex-1 accent-cyan-500"/><input aria-label={`${latencyLabel} cap in milliseconds`} type="number" min="0.1" step="any" placeholder="Auto" value={cap ?? ''} onChange={event => setCap(event.target.value === '' ? null : Math.max(0.1, Number(event.target.value)))} className="w-16 bg-transparent text-right text-[10px] text-slate-300 outline-none focus:ring-1 focus:ring-cyan-500"/><span className="text-[9px] text-slate-500">ms</span></div>
            </div>
        </div>}
        <div className="px-3 pb-5 sm:px-5">
        {data.length ? <div className="h-[420px] min-w-0"><ResponsiveContainer width="100%" height="100%" initialDimension={{width: 800, height: 420}}><ScatterChart margin={{top: 25, right: 25, bottom: 35, left: 15}}>
            <CartesianGrid stroke="#263249" strokeDasharray="3 5"/>
            <XAxis type="number" dataKey="x" domain={logScale ? ['auto', cap ?? 'auto'] : [0, cap ?? 'auto']} scale={logScale ? 'log' : 'auto'} axisLine={false} tickLine={false} tick={{fill:'#94a3b8',fontSize:10}} label={{value:`${latencyLabel} (ms)`,position:'bottom',fill:'#94a3b8',fontSize:11}}/>
            <YAxis type="number" dataKey="y" domain={[0, 'auto']} width={65} axisLine={false} tickLine={false} tick={{fill:'#94a3b8',fontSize:10}} label={{value:unit,angle:-90,position:'insideLeft',fill:'#94a3b8',fontSize:10}}/>
            <Tooltip cursor={{stroke:'#64748b',strokeDasharray:'3 5'}} content={({active,payload}) => { const point=payload?.[0]?.payload; return active && point ? <ChartTooltip title={shortCase(point.run)}><ChartTooltipRow label={`${latencyLabel} ${point.stat.toUpperCase()}`} value={metricNumber(point.x)} unit=" ms"/><ChartTooltipRow label={outputLabel} value={metricNumber(point.y)} unit={` ${unit}`}/><p className="mt-2 text-xs text-slate-400">{point.run.load ?? 'Full run'} {point.run.load == null ? '' : point.run.loadKind==='rate'?'req/s':'concurrent'} · Input {point.run.isl ?? '—'} / Output {point.run.osl ?? '—'} tokens</p></ChartTooltip> : null; }}/>
            {series.map(group => { const id = group.points[0].run.caseId; return <Scatter key={group.key} data={group.points} name={`${id} ${group.points[0].stat}`} line={group.points.length > 1 ? {stroke: caseColor(runs, id), strokeWidth: 1, strokeOpacity:.4} : false} isAnimationActive={false} shape={({cx,cy,payload}) => { const active=payload.run.id===selected?.id; return <g role="button" tabIndex={0} aria-label={`Inspect benchmark point ${payload.run.id} ${payload.stat.toUpperCase()}`} onClick={()=>onSelect(payload.run.id)} onKeyDown={event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();onSelect(payload.run.id);}}} style={{cursor:'pointer'}}>{active && <circle cx={cx} cy={cy} r={12} fill={caseColor(runs,id)} opacity={.15}/>}{statShape(payload.stat,cx,cy,active?5:4,{fill:caseColor(runs,id),stroke:active?'#f8fafc':caseColor(runs,id),strokeWidth:active?1.5:1})}</g>; }}/>; })}
        </ScatterChart></ResponsiveContainer></div> : <p role="status" className="py-20 text-center text-sm text-slate-400">No recorded measurements match these filters. Choose another percentile, axis or cap.</p>}
        <div className="flex flex-wrap justify-between gap-4"><div className="flex flex-wrap gap-x-5 gap-y-2">{cases.map((id,index)=><span key={id} className="inline-flex items-center gap-2 text-xs text-slate-400"><span className="h-2 w-2 rounded-full" style={{background:caseColor(runs,id)}}/>C{index+1} · {shortCase(points.find(r=>r.caseId===id))}</span>)}</div><div className="flex gap-3">{stats.map(stat => <span key={stat} className="inline-flex items-center gap-1 text-[10px] text-slate-400"><svg width="14" height="14" aria-hidden="true">{statShape(stat,7,7,3,{fill:'#a78bfa'})}</svg>{stat.toUpperCase()}</span>)}</div></div>
        {measured.length < points.length * stats.length && <p className="mt-3 text-xs text-slate-500">Missing measurements{perChip ? ' or accelerator counts' : ''}{logScale ? ' and non-positive log values' : ''} are omitted; percentiles are never substituted.</p>}
        {cap != null && data.length < measured.length && <p className="mt-2 text-xs text-slate-500">{measured.length - data.length} values exceed the {metricNumber(cap)} ms cap.</p>}

        </div>
    </section>;
}
