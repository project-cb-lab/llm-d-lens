import OptimizationConfiguration from './OptimizationConfiguration';
import { ArrowLeft, Pencil } from 'lucide-react';

export default function EvaluationConfigurationEditor({ onEditSetup, onUseSavedConfigurations, busy, ...props }) {
    const context = props.sharedContext || {};
    return (
        <div className="space-y-3">
            {(props.onCancel || onUseSavedConfigurations) && <div className="flex flex-wrap items-center gap-3">
            {props.onCancel && <button type="button" disabled={busy} onClick={props.onCancel} className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:border-cyan-500/40 hover:text-cyan-200"><ArrowLeft className="h-3.5 w-3.5" />Back</button>}
            {onUseSavedConfigurations && <button type="button" disabled={busy} onClick={onUseSavedConfigurations} className="inline-flex items-center rounded-lg border border-slate-700 px-3 py-2 text-xs text-cyan-300 hover:border-cyan-500/40">Use saved configurations</button>}
            </div>}
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-xl border border-slate-800 bg-slate-950/45 px-4 py-3 text-xs text-slate-200">
                <span><span className="text-slate-500">Model:</span> {context.model || '—'}</span>
                <span><span className="text-slate-500">Cluster:</span> {context.cluster?.name || context.cluster?.id || '—'}</span>
                <span><span className="text-slate-500">Runtime:</span> {context.modelServer || '—'}</span>
                <span className="min-w-0 break-all"><span className="text-slate-500">Image:</span> {context.image || '—'}</span>
                {onEditSetup && <button type="button" disabled={busy} onClick={onEditSetup} title="Edit setup" aria-label="Edit setup" className="ml-auto inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-cyan-300 hover:bg-cyan-500/10 hover:text-cyan-200"><Pencil className="h-3.5 w-3.5" /></button>}
            </div>
            <div data-evaluation-configuration-editor className="evaluation-configuration-editor">
                <OptimizationConfiguration {...props} embedded />
            </div>
        </div>
    );
}
