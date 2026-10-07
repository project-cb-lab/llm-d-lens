import React from 'react';
import { Search as SearchIcon } from 'lucide-react';

// Shared dark panel shell for Administration pages, matching the AIProviders
// page chrome (rounded slate card + toolbar row + titled table header).
export function AdminPanel({ icon: Icon, title, count, toolbar, children }) {
    return (
        <section className="relative z-10 flex flex-col gap-3.5 rounded-3xl border border-slate-900/90 bg-[#070b13]/65 p-5 shadow-2xl backdrop-blur-md">
            {toolbar}
            <div className="flex min-h-[52px] items-center justify-between border-b border-slate-800/60 px-1 py-2.5">
                <div className="flex items-center gap-2">
                    {Icon && <Icon className="h-4 w-4 text-cyan-400" />}
                    <h2 className="text-sm font-bold text-slate-300">{title}</h2>
                    {typeof count === 'number' && (
                        <span className="rounded-md bg-slate-900 px-2 py-0.5 text-[9px] text-slate-500">{count} shown</span>
                    )}
                </div>
            </div>
            {children}
        </section>
    );
}

/** Toolbar row used above a panel's table (search + filters + clear). */
export function AdminToolbar({ children }) {
    return (
        <div className="relative z-20 flex flex-wrap items-center gap-3 rounded-2xl border border-slate-800/60 bg-[#0a0f1d] p-3 shadow-md">
            {children}
        </div>
    );
}

/** Dark search input matching the other module toolbars. */
export function AdminSearch({ value, onChange, placeholder = 'Search…' }) {
    return (
        <div className="min-w-[240px] flex-1">
            <div className="relative">
                <SearchIcon className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                <input
                    type="search"
                    value={value}
                    onChange={(event) => onChange(event.target.value)}
                    placeholder={placeholder}
                    className="h-9 w-full rounded-xl border border-slate-800/60 bg-[#0b0f17] pl-9 pr-4 text-xs font-medium text-slate-200 outline-none focus:border-cyan-500/40"
                />
            </div>
        </div>
    );
}

export function AdminClearButton({ show, onClick }) {
    if (!show) return null;
    return (
        <button
            type="button"
            onClick={onClick}
            className="h-8 rounded-lg border border-slate-700 px-3 text-[10px] text-slate-300"
        >
            Clear Filters
        </button>
    );
}
