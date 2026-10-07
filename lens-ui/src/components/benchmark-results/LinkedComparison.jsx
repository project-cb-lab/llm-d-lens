import { useState } from 'react';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import ExperimentMap from './ExperimentMap.jsx';
import { caseColor, comparisonLines, experimentPoints, metricNumber, shortCase } from './linkedExperiment.js';
import { readResultMetric } from './resultExplorer.js';
const tooltip={background:'#111c2d',border:'1px solid #334155',borderRadius:10,color:'#cbd5e1',fontSize:11};
const axisStyle={stroke:'#53647c',tick:{fontSize:10},tickLine:false,axisLine:false};
const control='rounded-md border border-slate-700/70 bg-[#0b1422] px-2.5 py-1.5 text-[11px] text-slate-300 outline-none focus:border-cyan-400';
const metrics=[['throughput','Output throughput'],['ttft','TTFT'],['ntpot','NTPOT'],['itl','ITL'],['tpot','TPOT'],['e2e','End-to-end'],['successRate','Success rate'],['goodput','Per-request SLO goodput']];
export default function LinkedComparison({runs,details,selected,onSelect,initialAxis='isl',metricOptions=metrics,showMeasurements=true,stat:controlledStat}) {
 const [metric,setMetric]=useState('throughput'),[localStat,setStat]=useState('p95'),[axis,setAxis]=useState(initialAxis),[hidden,setHidden]=useState([]),[kind,setKind]=useState('');
 const stat=controlledStat ?? localStat;
 const points=experimentPoints(runs),kinds=[...new Set(points.map(p=>p.loadKind).filter(Boolean))];
 const effectiveKind=kinds.includes(kind)?kind:kinds[0];
 const compatible=points.filter(p=>!effectiveKind||p.loadKind===effectiveKind);
 const hasAxis = value => compatible.some(point => Number.isFinite(point[value]));
 const effectiveAxis = hasAxis(axis) ? axis : hasAxis('load') ? 'load' : 'isl';
 const lines=comparisonLines(compatible,effectiveAxis,metric,stat);
 const hasData=lines.some(line=>line.points.some(point=>Number.isFinite(point.x)&&Number.isFinite(point.y)));
 const hasAnyData=metricOptions.some(([key])=>compatible.some(point=>(Number.isFinite(point.load)||Number.isFinite(point.isl))&&(controlledStat != null ? [stat] : ['mean','p50','p90','p95','p99']).some(value=>Number.isFinite(readResultMetric(point,key,value).value))));
 if(!hasAnyData)return showMeasurements && points.length ? <ExperimentMap runs={runs} details={details} selected={selected} onSelect={onSelect} metric={metric} stat={stat}/> : null;
 const unit=readResultMetric(points[0],metric,stat).unit;
 const seriesLabel=line=>`${shortCase(line.points[0]?.run)} · ${line.caseId} · ${effectiveAxis==='load'?`ISL ${line.isl??'—'}`:`${line.load??'—'} ${line.loadKind==='rate'?'req/s':'concurrent'}`} · OSL ${line.osl??'—'}`;
 return <section className="min-w-0 overflow-hidden rounded-xl border border-slate-800 bg-[#0e1628]" aria-label="Linked comparison">
  <div className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-800 bg-[#131e31] px-4 py-3"><div><h3 className="flex items-center gap-2 text-sm font-medium text-slate-100">Load & input length comparison</h3><p className="mt-1 text-[10px] text-slate-500">{lines.length} curves · each line holds configuration and {effectiveAxis==='load'?'input/output lengths':'load and output length'} fixed · click a legend below to show or hide its curve</p></div><div className="flex flex-wrap gap-2"><select aria-label="Comparison metric" className={control} value={metric} onChange={e=>setMetric(e.target.value)}>{metricOptions.map(([key,label])=><option key={key} value={key}>{label}</option>)}</select><select aria-label="Comparison statistic" className={control} value={stat} disabled={controlledStat != null || !['ttft','ntpot','itl','tpot','e2e'].includes(metric)} onChange={e=>setStat(e.target.value)}>{['mean','p50','p90','p95','p99'].map(s=><option key={s}>{s}</option>)}</select><select aria-label="Comparison horizontal axis" className={control} value={effectiveAxis} onChange={e=>setAxis(e.target.value)}><option value="isl">X: Input length</option><option value="load">X: {effectiveKind==='rate'?'Request rate':'Concurrency'}</option></select>{kinds.length>1 && <select aria-label="Comparison load kind" className={control} value={effectiveKind} onChange={e=>setKind(e.target.value)}>{kinds.map(k=><option key={k}>{k}</option>)}</select>}</div></div>
  {hasData && <><div className="min-w-0 px-4 pt-3">
   <div className="min-w-0"><p className="mb-2 text-[10px] text-slate-500">{metricOptions.find(m=>m[0]===metric)?.[1]} ({unit})</p><div className="h-[240px] sm:h-[280px]" data-testid="all-condition-chart"><ResponsiveContainer width="100%" height="100%" initialDimension={{width:850,height:280}}><LineChart margin={{top:15,right:20,bottom:12,left:0}}>
    <CartesianGrid stroke="#263249" strokeDasharray="3 5"/><XAxis dataKey="x" type="number" {...axisStyle} domain={['dataMin','dataMax']} allowDuplicatedCategory={false} label={{value:effectiveAxis==='isl'?'Input tokens':effectiveKind==='rate'?'Offered requests / s':'Concurrent requests',position:'insideBottom',offset:-9,fill:'#64748b',fontSize:10}}/><YAxis {...axisStyle} width={65}/><Tooltip contentStyle={tooltip} labelFormatter={v=>`${effectiveAxis==='isl'?'Input':'Load'} ${v}`} formatter={(v,name)=>[metricNumber(v),name]}/>
    {lines.map((line,index)=><Line key={line.key} data={line.points.filter(p=>Number.isFinite(p.x))} dataKey="y" name={seriesLabel(line)} hide={hidden.includes(line.key)} stroke={caseColor(runs,line.caseId)} strokeDasharray={['','5 3','2 3','9 3 2 3'][index%4]} strokeWidth={1.7} connectNulls={false} isAnimationActive={false} dot={{r:3.5}} activeDot={{r:6}}/>)}
   </LineChart></ResponsiveContainer></div></div>
  </div>
  <div className="mx-4 mt-2 border-t border-slate-800/70 py-3">
   <div className="mb-2 flex items-center justify-between text-[11px] text-slate-500"><span>Curves · click to show or hide</span><button type="button" onClick={()=>setHidden([])} className="text-cyan-300 hover:text-cyan-200">Show all</button></div>
   <div role="group" aria-label="Comparison curves" className="flex max-h-32 flex-wrap gap-1.5 overflow-y-auto">{lines.map((line,index)=><button type="button" key={line.key} aria-label={`Toggle ${seriesLabel(line)}`} aria-pressed={!hidden.includes(line.key)} onClick={()=>setHidden(current=>current.includes(line.key)?current.filter(key=>key!==line.key):[...current,line.key])} className={`inline-flex items-center gap-2 rounded-md border px-2 py-1.5 text-left text-[11px] focus-visible:outline-2 focus-visible:outline-cyan-300 ${hidden.includes(line.key)?'border-slate-800 text-slate-600':'border-slate-700 bg-slate-900/50 text-slate-300'}`}>
    <svg width="28" height="10" className="shrink-0" aria-hidden="true"><line x1="0" y1="5" x2="28" y2="5" stroke={hidden.includes(line.key)?'#475569':caseColor(runs,line.caseId)} strokeWidth="2" strokeDasharray={['','5 3','2 3','9 3 2 3'][index%4]}/></svg>
    {seriesLabel(line)}
   </button>)}</div>
  </div>
  </>}
  {showMeasurements && <div className="px-4 pb-4"><ExperimentMap runs={runs} details={details} selected={selected} onSelect={onSelect} metric={metric} stat={stat}/></div>}
 </section>;
}
