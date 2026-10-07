import React, { useMemo, useRef, useState } from 'react';
import { Download, FileCode2, Files, Loader2, Table2 } from 'lucide-react';
import { buildResultExportState, downloadResultFile, resolveResultExport } from './resultExports.js';

export default function ResultExports({ details, run }) {
    const state = useMemo(() => buildResultExportState(details, run), [details, run]);
    const [operation, setOperation] = useState(null);
    const menu = useRef(null);
    const current = operation?.id === state.id ? operation : null;
    const exportFile = async kind => {
        setOperation({ id: state.id, busy: kind });
        try {
            downloadResultFile(await resolveResultExport(state, kind));
            setOperation({ id: state.id });
            if (menu.current) menu.current.open = false;
        } catch (error) {
            setOperation({ id: state.id, error: error instanceof Error ? error.message : 'Download failed.' });
        }
    };
    const items = [
        { key: 'bundle', label: 'Deployment ZIP', icon: Files, disabled: !state.artifactId, title: state.bundleReason },
        { key: 'inputs', label: 'Benchmark inputs YAML', icon: FileCode2, disabled: !Object.keys(state.benchmark).length, title: 'Original benchmark inputs for the selected configuration.' },
        { key: 'targets', label: 'Performance targets YAML', icon: FileCode2, disabled: !state.targets || !Object.keys(state.targets).length, title: state.targets ? 'Saved performance targets for the selected configuration.' : 'Performance targets were not recorded for this case.' },
        { key: 'results', label: 'Benchmark results CSV', icon: Table2, disabled: !state.results.some(result => Object.keys(result.metrics).length), title: 'All saved cases and load points in this benchmark.' },
    ];
    return <details ref={menu} className="relative" onKeyDown={event => { if (event.key === 'Escape') { menu.current.open = false; menu.current.querySelector('summary')?.focus(); } }} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) menu.current.open = false; }}>
        <summary aria-label="Download benchmark files" title="Download benchmark files" className="flex cursor-pointer list-none items-center rounded-lg border border-slate-700 p-2 text-cyan-300 hover:border-cyan-400 [&::-webkit-details-marker]:hidden"><Download size={18}/></summary>
        <div className="absolute right-0 z-40 mt-2 w-64 rounded-xl border border-slate-700 bg-[#101a2b] p-2 shadow-2xl">
            <p className="px-3 py-2 text-[10px] text-slate-500">Selected configuration · CSV includes all results</p>
            {items.map(({ key, label, icon: Icon, disabled, title }) => <button key={key} type="button" disabled={disabled || Boolean(current?.busy)} title={title} onClick={() => exportFile(key)} className="flex w-full items-center gap-3 rounded-lg px-3 py-3 text-left text-xs text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40">{current?.busy === key ? <Loader2 size={15} className="animate-spin"/> : React.createElement(Icon, { size: 15 })}<span>{label}</span></button>)}
            {current?.error && <p role="alert" className="px-3 py-2 text-xs text-rose-300">{current.error}</p>}
        </div>
    </details>;
}
