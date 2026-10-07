import { useEffect, useId, useRef, useState } from 'react';
import { FileCode, Loader2 } from 'lucide-react';
import { recommendationBudget } from '../../features/evaluation/experimentDesign.js';

export default function ResourceBasisControl({ value, onGenerate, hardware, disabled, loading = false, requiredCards = 1, label = 'Generate YAML', confirmLabel = 'Confirm & generate' }) {
    const [open, setOpen] = useState(false);
    const [draft, setDraft] = useState(value);
    const root = useRef(null);
    const trigger = useRef(null);
    const choices = useRef(null);
    const radioName = useId();
    const budget = recommendationBudget(hardware, draft);
    const insufficient = budget === 0 || budget < requiredCards;
    const close = () => { setOpen(false); trigger.current?.focus(); };

    useEffect(() => {
        if (!open) return;
        choices.current?.querySelector('input:checked')?.focus();
        const outside = event => { if (!root.current?.contains(event.target)) setOpen(false); };
        document.addEventListener('pointerdown', outside);
        return () => document.removeEventListener('pointerdown', outside);
    }, [open]);

    return <div ref={root} className="relative inline-flex" onKeyDown={event => {
        if (event.key === 'Escape' && open) { event.preventDefault(); close(); }
    }}>
        <button ref={trigger} type="button" aria-haspopup="dialog" aria-expanded={open} disabled={disabled}
            onClick={(event) => { event.preventDefault(); event.stopPropagation(); if (open) close(); else { setDraft(value); setOpen(true); } }}
            className="relative inline-flex items-center justify-center gap-2 rounded-lg bg-cyan-400 px-4 py-2.5 text-xs font-semibold text-slate-950 transition hover:bg-cyan-300 disabled:opacity-40">
            {loading ? <Loader2 size={15} className="animate-spin" aria-hidden="true" /> : <FileCode size={15} aria-hidden="true" />}
            {label}
        </button>
        {open && <div role="dialog" aria-label="Choose resource budget before generating YAML"
            className="absolute bottom-full right-0 z-50 mb-3 w-72 max-w-[calc(100vw-2rem)] rounded-xl border border-slate-700 bg-[#111b2c] p-4 shadow-2xl">
            <h3 className="text-xs font-semibold text-slate-100">Replica / TP resource budget</h3>
            <p className="mt-1 text-[10px] text-slate-500">Cards required by one deployment. Comparison targets run sequentially; keeping deployments can accumulate card usage.</p>
            <fieldset ref={choices} className="mt-3 space-y-1">
                <legend className="sr-only">Resource budget</legend>
                {[['available', 'Currently available'], ['total', 'All physical cards']].map(([key, text]) => <label key={key}
                    className={`flex cursor-pointer items-center gap-2 rounded-lg px-2 py-3 text-xs ${draft === key ? 'bg-cyan-400/10 text-cyan-100' : 'text-slate-300 hover:bg-slate-800'}`}>
                    <input type="radio" name={radioName} value={key} checked={draft === key} onChange={() => setDraft(key)} disabled={disabled} className="accent-cyan-400" />
                    <span className="flex-1">{text}</span><span className="tabular-nums text-cyan-200">{recommendationBudget(hardware, key)} cards</span>
                </label>)}
            </fieldset>
            {draft === 'total' && <p className="mt-2 text-[10px] leading-4 text-slate-500">Includes busy cards; scheduling may wait.</p>}
            <div className="mt-4 border-t border-slate-700/60 pt-3">
                <div className="flex justify-between text-[11px] text-slate-300"><span>Per deployment / budget</span><span>{requiredCards} / {budget} cards</span></div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-800"><div className={`h-full ${insufficient ? 'bg-rose-400' : 'bg-cyan-400'}`} style={{width: `${Math.min(100, budget ? requiredCards / budget * 100 : 100)}%`}} /></div>
                {insufficient && <p role="alert" className="mt-3 text-[11px] leading-5 text-rose-300">Insufficient resources: {budget === 0 ? 'no cards are available in this budget.' : `${requiredCards - budget} more cards are needed.`} Reduce replicas / TP or choose a sufficient budget.</p>}
            </div>
            <div className="mt-4 flex items-center justify-end gap-3">
                <button type="button" onClick={(event) => { event.preventDefault(); event.stopPropagation(); close(); }} className="text-[11px] text-slate-400">Cancel</button>
                <button type="button" disabled={disabled || insufficient} onClick={(event) => { event.preventDefault(); event.stopPropagation(); if (insufficient) return; close(); onGenerate(draft); }}
                    className="rounded-md bg-cyan-400 px-3 py-2 text-[11px] font-semibold text-slate-950 disabled:opacity-40">{confirmLabel}</button>
            </div>
        </div>}
    </div>;
}
