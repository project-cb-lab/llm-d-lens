import React, { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';

/** An overlay inspector: leaves the underlying result visible at desktop widths. */
export function SideDrawer({ title, subtitle, onClose, children }) {
    const panel = useRef(null);
    const close = useRef(onClose);
    const titleId = useId();
    useEffect(() => { close.current = onClose; }, [onClose]);
    useEffect(() => {
        const previousFocus = document.activeElement;
        const previousOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        panel.current?.focus();
        const onKey = event => {
            if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                close.current?.();
            }
            if (event.key !== 'Tab') return;
            event.stopPropagation();
            const controls = [...(panel.current?.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), summary, [tabindex="0"]') || [])].filter(el => el.getClientRects().length);
            const first = controls[0], last = controls[controls.length - 1];
            if (!first) { event.preventDefault(); panel.current?.focus(); return; }
            if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { event.preventDefault(); last.focus(); }
            if (!event.shiftKey && (document.activeElement === last || !panel.current.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
        };
        window.addEventListener('keydown', onKey, true);
        return () => {
            document.body.style.overflow = previousOverflow;
            window.removeEventListener('keydown', onKey, true);
            if (previousFocus?.isConnected) previousFocus.focus?.();
        };
    }, []);
    if (typeof document === 'undefined') return null;
    return createPortal(<div className="fixed inset-0 z-[210]">
        <div aria-hidden="true" onClick={onClose} className="absolute inset-0 bg-slate-950/20" />
        <section ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby={titleId} className="benchmark-inspector-drawer absolute inset-y-0 right-0 flex w-full flex-col border-l border-slate-700 bg-[#0b1423] text-slate-100 shadow-2xl shadow-black/50 outline-none sm:w-[480px] sm:max-w-[85vw]">
            <header className="flex shrink-0 items-start justify-between gap-4 border-b border-slate-800 px-6 py-5">
                <div className="min-w-0"><p className="mb-2 text-[10px] font-semibold uppercase tracking-widest text-sky-300">{subtitle || 'Recorded evidence'}</p><h2 id={titleId} className="break-words text-base font-semibold">{title}</h2></div>
                <button type="button" onClick={onClose} aria-label="Close inspector" className="rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-white focus-visible:outline-sky-400"><X size={18} /></button>
            </header>
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-6">{children}</div>
        </section>
    </div>, document.body);
}
