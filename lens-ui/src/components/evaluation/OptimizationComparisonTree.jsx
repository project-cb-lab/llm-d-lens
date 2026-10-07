import { useId } from 'react';
import { optimizationOptions, toggleOptimizationSelection } from '../../features/evaluation/experimentDesign.js';

const sceneColors = ['#22d3ee', '#a78bfa', '#34d399', '#fbbf24', '#fb7185', '#60a5fa', '#e879f9'];

export default function OptimizationComparisonTree({ guide, selected = ['full'], onChange, disabled = false }) {
    const gradient = useId().replaceAll(':', '');
    const options = optimizationOptions(guide);
    const colorFor = id => sceneColors[options.findIndex(option => option.id === id) % sceneColors.length];
    const routed = options.filter(option => !['direct-vllm', 'kubernetes-service'].includes(option.id));
    const direct = options.filter(option => ['direct-vllm', 'kubernetes-service'].includes(option.id));
    const height = Math.max(350, routed.length * 65 + 60);
    const hubY = 35 + (routed.length - 1) * 32.5;
    const toggle = id => { if (!disabled) onChange(toggleOptimizationSelection(selected, id)); };
    const leaf = (option, x, y, originX, originY) => {
        const active = selected.includes(option.id);
        const color = colorFor(option.id);
        return <g key={option.id}>
            <path d={`M${originX},${originY} C${originX + 60},${originY} ${x - 70},${y} ${x},${y}`} fill="none" stroke={active ? color : '#263449'} strokeWidth={active ? 2 : 1} />
            <g role="button" tabIndex={disabled ? -1 : 0} aria-disabled={disabled} aria-pressed={active} aria-label={`Compare ${option.label}: ${option.summary}`} onClick={() => toggle(option.id)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(option.id); } }} className="cursor-pointer outline-none focus-visible:stroke-white">
                <rect x={x-25} y={y-26} width="230" height="52" fill="transparent"/>
                <title>{`${option.summary}. ${option.components.join(' · ')}. Click to ${active ? 'remove from' : 'add to'} comparison.`}</title>
                <circle cx={x} cy={y} r={19} fill={active ? color : '#101c2c'} fillOpacity={active ? .09 : 1} stroke={active ? color : '#344358'} />
                {active && <circle cx={x} cy={y} r={25} fill="none" stroke={color} strokeOpacity=".2" />}
                <circle cx={x} cy={y} r={5} fill={active ? color : '#50627a'} />
                <text x={x + 32} y={y - 3} fill={active ? '#e0f2fe' : '#94a3b8'} fontSize="12" fontWeight="500">{option.label}</text>
                <text x={x + 32} y={y + 14} fill={active ? color : '#52647c'} fontSize="10">{option.summary}</text>
            </g>
        </g>;
    };
    return <section aria-label="Optimization comparisons" className="mt-5 min-w-0 border-t border-slate-800/70 pt-5">
        <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-xs font-semibold text-slate-200">Optimization comparisons</h3><p className="mt-1 max-w-2xl text-xs leading-5 text-slate-400">Compare serving scenarios with the same workload. Hover for details.</p></div><span aria-live="polite" className="text-xs text-cyan-300">{selected.length} scenarios selected</span></div>
        <div className="overflow-x-auto" style={{background: 'radial-gradient(ellipse at 48% 45%, #0e749012, transparent 65%)'}}>
            <svg role="group" aria-label="Serving scenario comparison tree" viewBox={`0 0 760 ${height}`} className="w-full min-w-[570px]" style={{maxHeight: 410}}>
                <defs><linearGradient id={gradient}><stop stopColor="#155e75"/><stop offset="1" stopColor="#22d3ee"/></linearGradient></defs>
                <path d={`M80,${height / 2} C150,${height / 2} 130,${hubY} 215,${hubY}`} stroke="#387386" strokeWidth="2" fill="none"/>
                {routed.map((option, index) => leaf(option, 440, 35 + index * 65, 235, hubY))}
                {direct.map((option, index) => leaf(option, 215 + index * 295, height - 38, 80, height / 2))}
                <circle cx="80" cy={height / 2} r="30" fill="#0c2033" stroke="#38bdf8" strokeOpacity=".65"/><circle cx="80" cy={height / 2} r="6" fill="#38bdf8"/>
                <text x="80" y={height / 2 - 43} textAnchor="middle" fill="#cbd5e1" fontSize="12">Same workload</text>
                <circle cx="225" cy={hubY} r="24" fill="#102331" stroke="#2a8e9e"/><text x="225" y={hubY + 4} textAnchor="middle" fill="#67e8f9" fontSize="11">EPP</text>
                <text x="225" y={hubY - 37} textAnchor="middle" fill="#64748b" fontSize="10">EPP scenarios</text>
            </svg>
        </div>
        <div aria-label="Selected scenario list" className="border-t border-slate-800/50 pt-3"><p className="text-xs text-slate-400">Selected scenarios · {selected.length} benchmark runs per topology</p><ul className="mt-3 flex flex-wrap gap-x-6 gap-y-3">{selected.map(id=><li key={id} className="flex items-center gap-2 text-sm text-slate-300"><span className="h-2 w-2 shrink-0 rounded-full" style={{background:colorFor(id)}}/>{options.find(option=>option.id===id)?.label}</li>)}</ul></div>
        {!selected.length && <p role="alert" className="mt-3 text-xs text-rose-300">Select at least one scenario.</p>}
    </section>;
}
