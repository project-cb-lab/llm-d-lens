import { CHART_SERIES } from '../ui/charts/palette.js';
import { readResultMetric } from './resultExplorer.js';
export const CASE_COLORS = CHART_SERIES;
export const metricNumber = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('en-US',{maximumFractionDigits:2}) : '—';
export const caseColor = (runs, id) => CASE_COLORS[Math.max(0,[...new Set(runs.map(r=>r.caseId))].indexOf(id)) % CASE_COLORS.length];
export function experimentPoints(runs) {
 const detailed = new Set(runs.filter(r=>r.stage || r.load!=null).map(r=>r.caseId));
 return runs.filter(r=>r.stage || r.load!=null || !detailed.has(r.caseId));
}
export function experimentHierarchy(runs) {
 const groups=new Map();
 for(const run of experimentPoints(runs)) {
  if(!groups.has(run.caseId)) groups.set(run.caseId,{id:run.caseId,loads:[]});
  const group=groups.get(run.caseId), key=JSON.stringify([run.loadKind,run.load]);
  let load=group.loads.find(l=>l.key===key);
  if(!load) {load={key,kind:run.loadKind,value:run.load,runs:[]};group.loads.push(load);}
  load.runs.push(run);
 }
 return [...groups.values()].map(g=>({...g,loads:g.loads.sort((a,b)=>(a.value??0)-(b.value??0))}));
}
/** Every line shares all dimensions other than its x axis. Repeats remain points. */
export function comparisonLines(runs, axis='load', metric='throughput', statistic='p95') {
 const groups=new Map();
 for(const run of experimentPoints(runs)) {
  const dims=axis==='load'?[run.caseId,run.loadKind,run.isl,run.osl]:[run.caseId,run.loadKind,run.load,run.osl];
  const key=JSON.stringify(dims);
  if(!groups.has(key)) groups.set(key,{key,caseId:run.caseId,loadKind:run.loadKind,load:run.load,isl:run.isl,osl:run.osl,points:[]});
  groups.get(key).points.push({x:run[axis],y:readResultMetric(run,metric,statistic).value,run});
 }
 return [...groups.values()].map(g=>({...g,points:g.points.sort((a,b)=>(a.x??0)-(b.x??0))}));
}
export function matchingRuns(runs, selected) {
 if(!selected)return [];
 return experimentPoints(runs).filter(r=>r.loadKind===selected.loadKind && r.load===selected.load && r.isl===selected.isl && r.osl===selected.osl && r.stage===selected.stage);
}
export const shortCase = run => {
 const c=run?.configuration||{}, d=c.decode||c.serving||{};
 const model=typeof c.model==='string'?c.model:c.model?.name;
 const replicas=d.replicaCount??c.replicas, tp=d.tensorParallelSize??c.tensor_parallel_size;
 const identity=run?.baselineType||c.routerProfile||run?.guide;
 const strategy={'affinity-only':'Prefix affinity','load-only':'Load scoring','router-neutral':'EPP neutral','router-round-robin':'EPP neutral','direct-vllm':'Direct vLLM','baseline-vllm':'Direct vLLM','kubernetes-service':'EPP bypass','optimized-baseline':'EPP full','pd-disaggregation':'P/D','tiered-prefix-cache':'Tiered cache','precise-prefix-cache-routing':'Precise index'}[identity];
 return `${run?.caseName||(model||run?.caseId||'Configuration').split('/').at(-1)}${replicas!=null?` · ${replicas}R`:''}${tp!=null?` × TP${tp}`:''}${strategy?` · ${strategy}`:''}`;
};
