import React, { useMemo, useState } from 'react';
import { Activity, X } from 'lucide-react';
import { buildExplorerRuns, getRunSeries, resolveExplorerGuide, GUIDE_EXPLORER_PROFILES } from './resultExplorer.js';
import LinkedComparison from './LinkedComparison.jsx';
import BenchmarkOverview from './BenchmarkOverview.jsx';
import BenchmarkScenario from './BenchmarkScenario.jsx';
import ResourceExplorer from './ResourceExplorer.jsx';
import HistoricalTimeRange from './HistoricalTimeRange.jsx';
import { AllCaseTelemetry } from './AllCaseMechanism.jsx';
import { experimentPoints, metricNumber, shortCase } from './linkedExperiment.js';
import { SideDrawer } from '../ui/SideDrawer.jsx';
const VIEWS=[['overview','Benchmark results',Activity],['compare','Compare',Activity]];
const CONTROL='rounded-md border border-slate-700/70 bg-[#0b1422] px-3 py-2 text-xs text-slate-300 outline-none focus:border-cyan-400';
function MetricInspector({metric,onClose}) {
 if(!metric)return null;
 return <SideDrawer title={metric.label} subtitle="Metric evidence" onClose={onClose}><p className="text-3xl text-cyan-200">{metricNumber(metric.value)} <small className="text-xs text-slate-500">{metric.value!=null?metric.unit:'Not collected'}</small></p><dl className="mt-6 space-y-4 text-xs leading-5">{[['Meaning',metric.definition],['Source',metric.source],['Field',metric.field],['Scope',typeof metric.scope==='object'?JSON.stringify(metric.scope):metric.scope],['Window',metric.window?`${metric.window.start} → ${metric.window.end}`:null],['Collection note',metric.reason],['Calculation',metric.formula]].filter(([,value])=>value).map(([label,value])=><div key={label}><dt className="text-slate-500">{label}</dt><dd className="mt-1 break-words text-slate-300">{value}</dd></div>)}</dl></SideDrawer>;
}
export default function GuideResultExplorer({details,guideType,view:controlledView,onViewChange}) {
 const runs=useMemo(()=>buildExplorerRuns(details),[details]);
 const hasResources = runs.some(r => getRunSeries(r).length > 0 || r.resources);
 const points=useMemo(()=>experimentPoints(runs),[runs]);
 const [selectedId,setSelectedId]=useState(''),[localView,setLocalView]=useState('overview'),[range,setRange]=useState(null),[timeMode,setTimeMode]=useState('elapsed'),[inspected,setInspected]=useState(null),[focusedCase,setFocusedCase]=useState(null);
 const requestedView=controlledView ?? localView;
 const visibleViews = VIEWS;
 const view=['resources','compare'].includes(requestedView)?requestedView:'overview';
 const setView=value=>{const next=value==='performance'?'overview':value; if(onViewChange)onViewChange(next);else setLocalView(next);};
 const run=runs.find(r=>r.id===selectedId)||points.find(r=>r.kind!=='baseline')||points[0];
 const candidateGuide=runs.find(r=>r.kind!=='baseline' && Object.hasOwn(GUIDE_EXPLORER_PROFILES,r.guide))?.guide || guideType || resolveExplorerGuide(details);
 const effectiveGuide=run?.kind==='baseline' ? candidateGuide : Object.hasOwn(GUIDE_EXPLORER_PROFILES,run?.guide)?run.guide:guideType;
 const profile=GUIDE_EXPLORER_PROFILES[effectiveGuide]||GUIDE_EXPLORER_PROFILES['optimized-baseline'];
 const select=id=>{setSelectedId(id);setRange(null);setInspected(null);const r=runs.find(r=>r.id===id);if(r)setFocusedCase(r.caseId);};
 const focusCase=id=>{setFocusedCase(id);const r=points.find(r=>r.caseId===id&&r.load===run?.load&&r.loadKind===run?.loadKind&&r.isl===run?.isl&&r.osl===run?.osl)||points.find(r=>r.caseId===id);if(r){setSelectedId(r.id);setRange(null);setInspected(null);}};
 const resourceRun=run && (getRunSeries(run).length ? run : runs.find(r=>r.caseId===run.caseId&&!r.stage)||run);
 const series=useMemo(()=>resourceRun?getRunSeries(resourceRun):[],[resourceRun]);
 const overviewProps={details,runs,guideType:effectiveGuide,selected:run,onSelect:select,onNavigate:setView,onInspect:setInspected,focusedCase,onCaseSelect:focusCase,onClear:()=>setFocusedCase(null),range,onRange:setRange};
 return <div className="min-w-0 space-y-5 text-slate-200">
    {view==='overview'&&<h2 title={profile.question} className="text-xs font-medium text-slate-500">{profile.title}</h2>}
    {view==='overview' && <BenchmarkScenario details={details} runs={runs} selected={run} onCaseSelect={focusCase} onNavigate={setView}/>}

  {controlledView==null&&<nav aria-label="Result analysis views" className="flex gap-2 overflow-x-auto border-b border-slate-800/70">{visibleViews.map(([key,label,Icon])=><button type="button" key={key} aria-current={view===key?'page':undefined} onClick={()=>setView(key)} className={`inline-flex shrink-0 items-center gap-2 border-b-2 px-3 pb-3 pt-1 text-[11px] transition ${view===key?'border-cyan-400 text-cyan-200':'border-transparent text-slate-500 hover:text-slate-200'}`}>{React.createElement(Icon,{size:14})}{label}</button>)}</nav>}
    {view==='compare'?<LinkedComparison runs={runs} details={details} selected={run} onSelect={select} initialAxis="load"/>:view==='overview'||!run?<BenchmarkOverview {...overviewProps}/>:<>
   <section aria-label="Resource controls" className="space-y-4 rounded-xl border border-slate-800 bg-[#0b1220] p-4">
   <div className="grid items-end gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_auto]">
    <label className="min-w-0 text-[10px] text-slate-500">Configuration<select aria-label="Selected configuration" className={`${CONTROL} mt-1 block w-full`} value={run.caseId} onChange={e=>focusCase(e.target.value)}>{[...new Set(points.map(r=>r.caseId))].map((id,i)=><option key={id} value={id}>C{i+1} · {shortCase(points.find(r=>r.caseId===id))}</option>)}</select></label>
    <label className="text-[10px] text-slate-500">Input / output tokens<select aria-label="Selected workload lengths" className={`${CONTROL} mt-1 block w-full`} value={`${run.isl}:${run.osl}`} onChange={e=>{const candidates=points.filter(r=>r.caseId===run.caseId&&`${r.isl}:${r.osl}`===e.target.value);select((candidates.find(r=>r.load===run.load&&r.loadKind===run.loadKind)||candidates[0]).id);}}>{[...new Map(points.filter(r=>r.caseId===run.caseId).map(r=>[`${r.isl}:${r.osl}`,r])).values()].map(r=><option key={`${r.isl}:${r.osl}`} value={`${r.isl}:${r.osl}`}>{r.isl??'—'} / {r.osl??'—'}</option>)}</select></label>
    <label className="text-[10px] text-slate-500">Load point<select aria-label="Selected load point" className={`${CONTROL} mt-1 block w-full`} value={run.id} onChange={e=>select(e.target.value)}>{points.filter(r=>r.caseId===run.caseId&&r.isl===run.isl&&r.osl===run.osl).map(r=><option key={r.id} value={r.id}>{r.load??'Full run'} {r.load==null?'':r.loadKind==='rate'?'req/s':'concurrent requests'}</option>)}</select></label>
    <button onClick={()=>setView('overview')} className="ml-auto inline-flex items-center gap-1 py-2 text-[10px] text-slate-500">All results</button>
   </div>
   {view==='resources'&&hasResources&&<>
    {series.length>0&&<HistoricalTimeRange compact key={resourceRun.id} data={series} range={range} onRange={setRange} timeMode={timeMode} onTimeMode={setTimeMode}/>}
   </>}
   </section>
   {view==='resources'&&hasResources&&<><ResourceExplorer hideHeader key={resourceRun.id} run={resourceRun} range={range} onRange={setRange} timeMode={timeMode}/><section className="border-t border-slate-800/60 pt-4"><h3 className="mb-4 text-sm font-medium text-slate-200">Compare full-run resource telemetry across configurations</h3><AllCaseTelemetry runs={runs} selected={run} onSelect={id=>{const r=runs.find(r=>r.id===id);if(r)focusCase(r.caseId);}} resource/></section></>}
  </>}
  {focusedCase&&view!=='overview'&&<button type="button" onClick={()=>{setFocusedCase(null);setView('overview');}} className="inline-flex items-center gap-2 text-[10px] text-slate-500"><X size={11}/>Return to all experiments</button>}
  {inspected&&<MetricInspector metric={inspected} onClose={()=>setInspected(null)}/>}
 </div>;
}
