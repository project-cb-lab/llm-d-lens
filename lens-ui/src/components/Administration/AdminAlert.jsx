import React from 'react';
import { cn } from '../../utils/cn';

const TONES = {
    error: 'border-rose-500/30 bg-rose-500/10 text-rose-200',
    success: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200',
    info: 'border-sky-500/30 bg-sky-500/10 text-sky-200',
};

/** Inline transient/error banner for Administration pages. */
export function AdminAlert({ tone = 'error', children }) {
    if (!children) return null;
    return (
        <p role="alert" className={cn('rounded-lg border px-3 py-2 text-xs', TONES[tone])}>
            {children}
        </p>
    );
}
