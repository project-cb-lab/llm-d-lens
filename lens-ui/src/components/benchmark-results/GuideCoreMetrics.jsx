import { useState } from 'react';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { GUIDE_FOCUS } from './guideFocus.js';
import { getRunSeries, readResultMetric } from './resultExplorer.js';
import { experimentPoints, metricNumber, shortCase } from './linkedExperiment.js';
import GuideMetricTable from './GuideMetricTable.jsx';

const colors = ['#22d3ee', '#a78bfa', '#fbbf24'];
const qualityLabel = {estimated:'Estimate', calculated:'Calculated'};

function Metric({metric, onInspect}) {
    return <div className="min-w-0 border-b border-slate-800/70 py-3">
        <button type="button" onClick={() => onInspect?.(metric)} className="text-left text-xs text-slate-300 underline decoration-slate-700 underline-offset-4" title={metric.reason || metric.source}>{metric.label}</button>
        <p className="mt-2 text-lg font-medium tabular-nums text-slate-100">{metric.value == null ? <span className="text-sm font-normal text-slate-500">Not collected</span> : <>{metricNumber(metric.value)} <span className="text-xs font-normal text-slate-400">{metric.unit}</span></>}</p>
        {qualityLabel[metric.quality] && <span className="mt-1 block text-[10px] text-amber-300">{qualityLabel[metric.quality]}</span>}
    </div>;
}

function EndpointLoads({run}) {
    const endpoints = (run.observability?.per_endpoint || []).filter(item => Number.isFinite(item.inflight_token_load?.mean));
    if (!endpoints.length) return null;
    const max = Math.max(1, ...endpoints.map(item => item.inflight_token_load.mean));
    return <section aria-label="Endpoint token load" className="mt-5 border-t border-slate-800 pt-4">
        <div className="flex items-center justify-between gap-3"><h4 className="text-sm text-slate-200">Token load by instance</h4><span tabIndex={0} title="Window means per endpoint, not simultaneous load. Compare instances with the same capacity." className="text-[11px] text-slate-500">Window mean · tokens</span></div>
        {<div className="mt-3 space-y-3">{endpoints.map(item => <div key={item.endpoint} className="grid grid-cols-[minmax(0,1fr)_minmax(60px,2fr)_auto] items-center gap-3 text-xs"><span className="break-all text-slate-400">{item.endpoint}</span><div className="h-2 rounded bg-slate-800"><div className="h-2 rounded bg-cyan-400" style={{width:`${Math.max(0,item.inflight_token_load.mean) / max * 100}%`}}/></div><span className="tabular-nums text-cyan-200">{metricNumber(item.inflight_token_load.mean)} tokens</span></div>)}</div>}
    </section>;
}

function MechanismTimeline({run, profile}) {
    const data = getRunSeries(run);
    const available = profile.series.filter(([key]) => data.some(point => Number.isFinite(point[key])));
    if (!available.length) return null;
    return <section className="mt-5 border-t border-slate-800 pt-4" aria-label="Guide mechanism timeline">
        <h4 className="text-sm text-slate-200">{available.map(([,label]) => label).join(' / ')} over time</h4>
        <p className="mt-1 text-xs text-slate-500">{profile.unit}</p>
        <div className="my-3 flex flex-wrap gap-4 text-xs">{available.map(([key,label],i) => <span key={key} style={{color:colors[i]}}>{label}</span>)}</div>
        <div className="h-52"><ResponsiveContainer width="100%" height="100%" initialDimension={{width:850,height:208}}><LineChart data={data} margin={{top:8,right:16,left:8,bottom:8}}><CartesianGrid stroke="#243044" vertical={false}/><XAxis dataKey="elapsed" type="number" domain={['dataMin','dataMax']} unit="s" tick={{fontSize:10}}/><YAxis tick={{fontSize:10}} tickFormatter={value=>Intl.NumberFormat('en',{notation:'compact'}).format(value)}/><Tooltip contentStyle={{background:'#0f172a',border:'1px solid #334155',color:'#e2e8f0'}} labelFormatter={value=>`${value}s`} formatter={(value,name)=>[`${metricNumber(value)} ${profile.unit}`,name]}/>{available.map(([key,label],i)=><Line key={key} dataKey={key} name={label} stroke={colors[i]} dot={false} connectNulls={false} isAnimationActive={false}/>)}</LineChart></ResponsiveContainer></div>
    </section>;
}

export default function GuideCoreMetrics({details, runs, selected, guideType, onSelect, onInspect}) {
    const [stat, setStat] = useState('p95');
    const [expanded, setExpanded] = useState(false);
    if (!selected) return null;
    const guide = Object.hasOwn(GUIDE_FOCUS, guideType) ? guideType : selected.guide;
    const profile = GUIDE_FOCUS[guide];
    if (!profile) return null;
    const points = experimentPoints(runs);
    const summaries = runs.filter(run=>!points.some(point=>point.id===run.id));
    const isSummary = summaries.some(run=>run.id===selected.id);
    const collection = selected.observability?.reason ? selected.observability : selected.caseObservability;
    const collectionScope = collection === selected.caseObservability ? 'Full-run telemetry' : 'Telemetry';
    const inspectPoint = id => {onSelect?.(id); setExpanded(true);};
    return <section aria-label="Guide core metrics" className="min-w-0 rounded-xl border border-slate-800 bg-slate-900/30 p-5">
        <header className="flex flex-wrap items-start justify-between gap-3">
            <h3 className="text-base font-semibold text-slate-100">{profile.title}</h3>
            <div role="group" aria-label="Latency percentile" className="flex rounded-lg border border-slate-700 p-0.5">{['p95','p99'].map(value=><button key={value} type="button" aria-pressed={stat===value} onClick={()=>setStat(value)} className={`rounded-md px-3 py-1.5 text-xs ${stat===value?'bg-cyan-900/70 text-cyan-100':'text-slate-400 hover:text-slate-100'}`}>{value.toUpperCase()}</button>)}</div>
        </header>
        {collection?.reason && ['empty','unavailable','failed'].includes(collection.status) && <span tabIndex={0} title={`${collectionScope}: ${collection.reason}`} className="mt-3 inline-block rounded border border-amber-900/50 px-2 py-1 text-xs text-amber-200">{collectionScope} unavailable</span>}
        <GuideMetricTable details={details} points={points} profile={profile} stat={stat} selected={selected} onSelect={inspectPoint} onInspect={onInspect}/>
        {summaries.length>0 && <div className="mt-4 flex flex-wrap items-center gap-2 text-xs"><span className="text-slate-500">Full-run evidence:</span>{summaries.map(run=><button key={run.id} type="button" aria-label={`View full-run telemetry ${run.caseId}`} onClick={()=>inspectPoint(run.id)} className="rounded border border-slate-800 px-2 py-1 text-slate-400 hover:border-slate-600 hover:text-slate-200">{shortCase(run)}{run.isl!=null ? ` · ${run.isl} / ${run.osl} tokens` : ''}</button>)}</div>}
        <details open={expanded} onToggle={event=>setExpanded(event.currentTarget.open)} className="mt-4 border-t border-slate-800 pt-4">
            <summary className="cursor-pointer text-xs text-slate-400">
                <span className="font-medium text-slate-200">Result details</span>
                <span className="ml-3" title={shortCase(selected)}>{isSummary?'Full case':selected.load==null?'Full run':`${selected.load} ${selected.loadKind==='rate'?'req/s':'concurrent'}`}</span>
                <span title={[selected.scopeLabel, selected.window && `${selected.window.start} → ${selected.window.end}`].filter(Boolean).join(' · ')} className="ml-2 inline-block rounded bg-slate-800 px-2 py-0.5 text-[10px]">{selected.stage?'Stage':'Full run'}</span>
            </summary>
            {guide==='optimized-baseline' && <EndpointLoads run={selected}/>}
            <MechanismTimeline run={selected} profile={profile}/>
            <h4 className="mt-5 text-sm font-medium text-slate-200">Request results</h4>
            <div className="grid gap-x-6 sm:grid-cols-2 xl:grid-cols-3">{['throughput','itl','tpot','e2e','inputThroughput','requestRate','requestCount','successCount','failureCount','successRate','goodput','duration'].map(key=><Metric key={key} metric={readResultMetric(selected,key,stat)} onInspect={onInspect}/>)}</div>
            <h4 className="mt-5 text-sm font-medium text-slate-200">{guide==='pd-disaggregation'?'P/D pool pressure and transfer cost':guide==='tiered-prefix-cache'?'Restore cost and capacity evidence':guide==='precise-prefix-cache-routing'?'Engine reuse and index evidence':'Cache reuse evidence'}</h4>
            <div className="grid gap-x-6 sm:grid-cols-2 xl:grid-cols-3">{[...(isSummary?profile.primary:[]),...profile.supporting].map(key=><Metric key={key} metric={readResultMetric(selected,key,stat)} onInspect={onInspect}/>)}</div>
        </details>
    </section>;
}
