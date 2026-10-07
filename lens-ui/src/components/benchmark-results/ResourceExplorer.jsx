import React, { useMemo, useState } from 'react';
import { SideDrawer } from '../ui/SideDrawer.jsx';
import { ArrowUpRight, Cpu, X } from 'lucide-react';
import { resourceSeries } from './resourceSeries.js';
import Timeline from './ResultTimeline.jsx';
import { downloadResultFile } from './resultExports.js';
import { acceleratorValue, utilizationLabel } from './acceleratorDisplay.js';

const finite = value => typeof value === 'number' && Number.isFinite(value);
const format = value => finite(value) ? value.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '—';
// Replaced with the run's actual accelerator label ("XPU utilization", …).
const UTILIZATION_PLACEHOLDER = 'GPU / XPU utilization';
const localizeUtilization = (lines, label) => lines.map(([key, text, ...rest]) => [key, text === UTILIZATION_PLACEHOLDER ? label : text, ...rest]);
const GROUPS = [
  { title: 'Request & token traffic', description: 'Prometheus rates over the saved benchmark window.', lines: [['request_rate_rps', 'Requests', 'req/s'], ['output_token_rate_tps', 'Output tokens', 'tok/s'], ['input_token_rate_tps', 'Input tokens', 'tok/s']] },
  { title: 'GPU activity & KV occupancy', lines: [['gpu_utilization_percent', 'GPU / XPU utilization', '%'], ['kv_cache_usage_percent', 'KV occupancy', '%'], ['gpu_cache_usage_percent', 'HBM KV occupancy', '%']] },
  { title: 'Device memory', lines: [['gpu_framebuffer_used_gib', 'Device framebuffer used', 'GiB'], ['gpu_memory_usage_gib', 'Engine GPU memory', 'GiB'], ['cpu_memory_usage_gib', 'Engine CPU memory', 'GiB']] },
  { title: 'Network traffic', description: 'Namespace network counters; not an isolated KV transfer measurement.', lines: [['network_receive_mib_s', 'Receive', 'MiB/s'], ['network_transmit_mib_s', 'Transmit', 'MiB/s']] },
  { title: 'Serving pressure', lines: [['queue_depth', 'Waiting requests', 'requests'], ['running_requests', 'In-flight requests', 'requests'], ['epp_inflight_requests', 'Router in-flight', 'requests']] },
  { title: 'Prefill / Decode load', lines: [['prefill_waiting_requests', 'Prefill waiting', 'requests'], ['decode_waiting_requests', 'Decode waiting', 'requests'], ['prefill_running_requests', 'Prefill in-flight', 'requests'], ['decode_running_requests', 'Decode in-flight', 'requests']] },
];
const POD_METRICS = [
  ['cpu_usage_cores', 'CPU', 'cores'], ['memory_working_set_bytes', 'Working set', 'GiB', 2 ** 30],
  ['gpu_utilization_percent', 'GPU / XPU utilization', '%'], ['gpu_framebuffer_used_bytes', 'Device memory', 'GiB', 2 ** 30], ['kv_cache_usage_percent', 'KV occupancy', '%'],
  ['request_rate_rps', 'Request rate', 'req/s'], ['waiting_requests', 'Waiting requests', 'requests'],
  ['running_requests', 'In-flight requests', 'requests'], ['output_token_rate_tps', 'Output', 'tok/s'],
  ['filesystem_read_bytes_per_second', 'Filesystem reads', 'MiB/s', 2 ** 20],
  ['filesystem_write_bytes_per_second', 'Filesystem writes', 'MiB/s', 2 ** 20],
];


export default function ResourceExplorer({ run, compact = false, hideHeader = false, onExpand, range: sharedRange, onRange, timeMode = 'elapsed' }) {
  const [localRange, setLocalRange] = useState(null);
  const range = onRange ? sharedRange : localRange;
  const setRange = onRange || setLocalRange;
  const [selectedPod, setSelectedPod] = useState(null);
  const [rankingKey, setRankingKey] = useState('cpu_usage_cores');
  const [search, setSearch] = useState('');
  const [role, setRole] = useState('all');
  const data = useMemo(() => resourceSeries(run), [run]);
  const utilization = utilizationLabel(acceleratorValue(run));
  const podMetrics = localizeUtilization(POD_METRICS, utilization);
  const groups = GROUPS
    .map(group => ({ ...group, lines: localizeUtilization(group.lines, utilization) }))
    .filter(group => group.lines.some(([key]) => data.some(row => finite(row[key]))));
  const observation = run.caseObservability || run.observability || {};
  const pods = Array.isArray(observation.per_pod) ? observation.per_pod : [];
  const roles = [...new Set(pods.map(pod => pod.role || 'Unclassified'))];
  const metric = podMetrics.find(([key]) => key === rankingKey);
  const ranking = pods.filter(pod => String(pod.pod).toLowerCase().includes(search.toLowerCase()) && (role === 'all' || (pod.role || 'Unclassified') === role))
    .sort((a, b) => (finite(b[rankingKey]?.mean) ? b[rankingKey].mean : -Infinity) - (finite(a[rankingKey]?.mean) ? a[rankingKey].mean : -Infinity));
  const maximum = Math.max(0, ...ranking.map(pod => finite(pod[rankingKey]?.mean) ? pod[rankingKey].mean : 0));
  const observedValues = pods.filter(pod => role === 'all' || (pod.role || 'Unclassified') === role).map(pod => pod[rankingKey]?.mean).filter(finite);
  const average = observedValues.length ? observedValues.reduce((sum, value) => sum + value, 0) / observedValues.length : null;
  const snapshot = run.resources;
  return <div className="space-y-4">
    {!hideHeader && <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="flex items-center gap-2 text-xs font-semibold text-slate-200"><Cpu size={15} className="text-sky-400" />Resources during benchmark</h3><p className="mt-1 text-[11px] text-slate-500">Historical samples · {run.scopeLabel} · {data.length} timestamps{range ? ` · ${format(range[0])}–${format(range[1])}s chart interval` : ''}</p></div>{compact ? <button onClick={onExpand} className="flex items-center gap-1 text-xs text-sky-300">Explore resources <ArrowUpRight size={13} /></button> : range && <button onClick={() => setRange(null)} className="text-xs text-sky-300">Reset interval</button>}</div>}

    {groups.length ? <div className="grid min-w-0 gap-4 xl:grid-cols-2">{(compact ? groups.slice(0, 2) : groups).map(group => <Timeline key={group.title} group={group} data={data} range={range} onRange={setRange} timeMode={timeMode} />)}</div> : <div className="rounded-xl border border-dashed border-slate-800 px-4 py-5 text-xs leading-5 text-slate-500">{observation.reason || run.observability?.reason || 'No Prometheus samples have been saved for this benchmark.'} {run.stage ? 'Select the full-case result to check case-wide telemetry.' : 'A completed task can still have no Prometheus samples. Enable deployment monitoring before the next run.'}</div>}
    {!compact && <>
      <section className="rounded-xl border border-slate-800 bg-[#0b1220] p-4">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-xs font-semibold text-slate-200">Pod resource ranking</h3><p className="mt-1 text-[11px] text-slate-500">Full-case window averages · highest first. Chart interval does not filter these averages.</p></div><div className="flex flex-wrap gap-2"><input aria-label="Find resource Pod" placeholder="Find Pod" value={search} onChange={e => setSearch(e.target.value)} className="w-36 rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs" /><select aria-label="Resource Pod role" value={role} onChange={e => setRole(e.target.value)} className="rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs"><option value="all">All roles</option>{roles.map(value => <option key={value}>{value}</option>)}</select><select aria-label="Pod ranking metric" value={rankingKey} onChange={e => setRankingKey(e.target.value)} className="rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs">{podMetrics.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div></div>
        <div><div className="overflow-x-auto"><table className="w-full min-w-[640px] text-left text-xs"><thead className="border-b border-slate-800 text-[10px] text-slate-500"><tr>{['Pod / role', `${metric[1]} mean (${metric[2]})`, 'Peak', 'vs Pod average'].map(label => <th scope="col" key={label} className="px-3 pb-3 font-medium">{label}</th>)}</tr></thead><tbody className="divide-y divide-slate-800/50">{ranking.map(pod => {
          const value = pod[rankingKey]?.mean, peak = pod[rankingKey]?.max;
          const change = finite(value) && finite(average) && average !== 0 ? (value - average) / Math.abs(average) * 100 : null;
          return <tr key={pod.pod} className={selectedPod?.pod === pod.pod ? 'bg-sky-950/30' : 'hover:bg-slate-800/30'}>
            <th scope="row" className="max-w-64 px-3 py-3 font-normal"><button type="button" onClick={() => setSelectedPod(pod)} className="block max-w-full text-left hover:text-sky-200"><span className="block truncate font-mono text-slate-300" title={pod.pod}>{pod.pod}</span><span className="mt-1 block text-[10px] text-slate-500">{pod.role || 'Unclassified'}</span></button></th>
            <td className="w-1/3 px-3 py-3"><div className="mb-2 tabular-nums text-sky-200">{format(finite(value) ? value / (metric[3] || 1) : null)}</div><div className="h-1.5 rounded-full bg-slate-800"><div className="h-full rounded-full bg-sky-500" style={{width: finite(value) && maximum > 0 ? `${Math.max(0,value / maximum * 100)}%` : '0%'}}/></div></td>
            <td className="px-3 py-3 tabular-nums text-slate-300">{format(finite(peak) ? peak / (metric[3] || 1) : null)}</td>
            <td className="px-3 py-3 tabular-nums text-slate-400">{change == null ? '—' : `${change > 0 ? '+' : ''}${format(change)}%`}</td>
          </tr>;
        })}</tbody></table>{!ranking.length && <p className="py-6 text-center text-xs text-slate-500">No matching per-Pod summaries were saved.</p>}</div>
          <p className="mt-3 text-[10px] text-slate-500">Compared with the mean of Pods in the selected role; search does not change this baseline. Peak values use the same unit as the mean.</p>
          {selectedPod && <SideDrawer title={selectedPod.pod} subtitle="Pod resource metrics · saved window" onClose={() => setSelectedPod(null)}><aside aria-label="Pod resource inspector" className="min-w-0 rounded-lg border border-sky-500/25 bg-slate-950/50 p-4"><div className="flex items-start justify-between gap-2"><h4 className="break-all font-mono text-xs text-sky-200">{selectedPod.pod}</h4><button aria-label="Close Pod inspector" onClick={() => setSelectedPod(null)}><X size={15} /></button></div><p className="mt-2 text-[10px] leading-5 text-slate-500">Saved full-case window: {observation.window?.start || 'unknown'} → {observation.window?.end || 'unknown'}. Per-Pod raw time series were not retained.</p><table className="mt-3 w-full text-left text-[10px]"><thead className="text-slate-500"><tr><th>Metric</th><th>Mean</th><th>Max</th></tr></thead><tbody>{podMetrics.filter(([key]) => selectedPod[key]).map(([key, label, unit, scale = 1]) => <tr key={key} className="border-t border-slate-800"><td className="py-2 text-slate-400">{label} ({unit})</td><td>{format(finite(selectedPod[key]?.mean) ? selectedPod[key].mean / scale : null)}</td><td>{format(finite(selectedPod[key]?.max) ? selectedPod[key].max / scale : null)}</td></tr>)}</tbody></table></aside></SideDrawer>}
        </div>
      </section>
      {snapshot && Object.keys(snapshot).length > 0 && <details className="rounded-xl border border-slate-800 bg-[#0b1220] p-4"><summary className="cursor-pointer text-xs font-semibold text-slate-300">Deployment resource snapshot · {snapshot?.pods?.length ?? 0} saved Pods</summary><button type="button" className="mt-3 rounded-lg border border-slate-700 px-3 py-2 text-xs text-cyan-200" onClick={() => downloadResultFile({blob: new Blob([JSON.stringify({case_id: run.caseId, scope: run.resourcesScope || 'Saved case-completion snapshot', snapshot}, null, 2)], {type: 'application/json'}), filename: `${String(run.caseId || 'benchmark').replace(/[^a-zA-Z0-9_-]/g, '-')}-resource-snapshot.json`})}>Download snapshot JSON</button><p className="mt-3 text-xs text-slate-400">Saved deployment objects for checking Pod placement and state after the benchmark.</p><p className="my-3 text-xs leading-5 text-slate-500">Pod phase describes its state when this snapshot was captured. A saved “Running” phase does not mean the benchmark is still running or that the Pod exists now.</p>{snapshot?.pods?.map(pod => <details key={pod.name} className="mt-2 rounded border border-slate-800 p-3"><summary className="cursor-pointer text-xs text-slate-400">{pod.name} · phase at capture: {pod.phase || 'not recorded'}</summary><pre className="mt-3 max-h-64 overflow-auto text-[11px] text-slate-500">{JSON.stringify(pod, null, 2)}</pre></details>)}</details>}
    </>}
  </div>;
}
