import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { CheckCircle2, Loader2, X, XCircle } from 'lucide-react';
import { cn } from '../../utils/cn';
import { Button } from '../ui/Button';

// Right-side, full-page-height log drawer for a data-plane operation. Streams
// the operation's log lines and final status.
export function GatewayOperationLogDrawer({ isOpen, onClose, title, status = 'running', lines = [], message = '' }) {
    const endRef = useRef(null);
    useEffect(() => {
        if (endRef.current) endRef.current.scrollIntoView({ block: 'end' });
    }, [lines.length, isOpen]);

    if (!isOpen || typeof document === 'undefined') return null;

    const icon = status === 'running'
        ? <Loader2 size={14} className="animate-spin text-sky-400" />
        : status === 'succeeded'
            ? <CheckCircle2 size={14} className="text-emerald-400" />
            : <XCircle size={14} className="text-rose-400" />;

    return createPortal(
        <>
            <button type="button" aria-label="Close log panel" className="fixed inset-0 z-[90] cursor-default bg-black/40" onClick={onClose} />
            <aside
                role="log"
                aria-live="polite"
                className="fixed inset-y-0 right-0 z-[100] flex h-full w-full max-w-xl flex-col border-l border-slate-700/70 bg-slate-950 shadow-2xl"
            >
                <header className="flex items-center gap-2 border-b border-slate-800 px-4 py-3">
                    {icon}
                    <span className="text-sm font-semibold text-slate-200">{title || 'Data-plane operation'}</span>
                    <span className={cn(
                        'rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
                        status === 'running' ? 'bg-sky-500/15 text-sky-300'
                            : status === 'succeeded' ? 'bg-emerald-500/15 text-emerald-300'
                                : 'bg-rose-500/15 text-rose-300',
                    )}>{status}</span>
                    <Button variant="ghost" size="icon" className="ml-auto" aria-label="Close" onClick={onClose}><X size={14} /></Button>
                </header>
                <div className="flex-1 overflow-y-auto bg-black/40 px-4 py-3 font-mono text-[11px] leading-relaxed text-slate-300">
                    {lines.length === 0 && <p className="text-slate-500">Starting…</p>}
                    {lines.map((line, index) => (
                        <div key={index} className="whitespace-pre-wrap break-words">{line}</div>
                    ))}
                    <div ref={endRef} />
                </div>
                {message && (
                    <footer className="border-t border-slate-800 px-4 py-3 text-xs text-slate-300">{message}</footer>
                )}
            </aside>
        </>,
        document.body,
    );
}

export default GatewayOperationLogDrawer;
