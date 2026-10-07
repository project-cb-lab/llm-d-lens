import { optimizationOptions } from '../../features/evaluation/experimentDesign.js';

export default function OptimizationSelectionSummary({ guide, selected = ['full'] }) {
    const options = optimizationOptions(guide);
    return <section aria-label="Selected comparison scenarios" className="mt-4 border-t border-slate-800/60 pt-3">
        <p className="text-xs text-slate-500">{selected.length} comparison {selected.length === 1 ? 'target' : 'targets'}</p>
        <ul className="mt-2 flex flex-wrap gap-y-2 text-xs leading-5 text-slate-300">
            {selected.map(id => <li key={id} className="break-words border-r border-slate-700 px-3 first:pl-0 last:border-r-0">{options.find(option => option.id === id)?.label || id}</li>)}
        </ul>
    </section>;
}
