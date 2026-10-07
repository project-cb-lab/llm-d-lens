import React from 'react';
import { Clock3, RotateCcw } from 'lucide-react';
import { ToggleGroup } from '../ui/ToggleGroup.jsx';
import { historicalBounds, historicalPreset } from './historicalRange.js';

export default function HistoricalTimeRange({ data, range, onRange, timeMode = 'elapsed', onTimeMode, compact = false }) {
  const bounds = historicalBounds(data);
  if (!bounds) return null;
  const interval = range || [bounds.start, bounds.end];
  const count = data.filter(point => point.elapsed >= interval[0] && point.elapsed <= interval[1]).length;
  const presets = [{ value: 'all', label: 'Full run' }, ...[60, 300, 900].filter(seconds => bounds.end - bounds.start > seconds).map(seconds => ({ value: String(seconds), label: `Final ${seconds / 60} min` }))];
  const preset = !range ? 'all' : presets.find(item => item.value !== 'all' && historicalPreset(data, Number(item.value)).every((value, i) => Math.abs(value - range[i]) < 0.001))?.value || 'custom';
  const selectRange = value => onRange(value);
  return <section aria-label="Historical time range" className={compact ? 'border-t border-slate-800 pt-3' : 'rounded-xl border border-sky-500/20 bg-gradient-to-r from-sky-950/30 to-slate-950/60 p-4'}>
    <div className="flex flex-wrap items-center justify-between gap-3"><div>{!compact && <h3 className="flex items-center gap-2 text-xs font-semibold text-sky-200"><Clock3 size={14} />Benchmark time window</h3>}<p className="mt-1 text-[10px] text-slate-500">{count} / {bounds.count} timestamps{!compact && ' · applies to time charts, not client percentiles or full-case Pod averages'}</p></div><div className="flex flex-wrap items-center gap-2"><ToggleGroup size="xs" options={presets} value={preset} onChange={value => selectRange(value === 'all' ? null : historicalPreset(data, Number(value)))} activeClassName="!bg-cyan-500/15 !text-cyan-200" className="!border-slate-700 !bg-slate-950" />{onTimeMode && <ToggleGroup size="xs" options={[{ value: 'elapsed', label: 'Elapsed' }, { value: 'utc', label: 'UTC' }]} value={timeMode} onChange={onTimeMode} className="!border-slate-700 !bg-slate-950" activeClassName="!bg-violet-500/15 !text-violet-200" />}<button type="button" title="Reset to full saved window" aria-label="Reset historical time range" onClick={() => selectRange(null)} className="rounded-lg border border-slate-700 p-1.5 text-slate-400 hover:text-sky-200"><RotateCcw size={13} /></button></div></div>
    {count === 0 && <p className="mt-2 text-xs text-amber-300">No saved samples fall in this interval.</p>}
  </section>;
}
