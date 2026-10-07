// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import React from 'react';
import { cn } from '../../utils/cn';

const TONES = {
    neutral: {
        icon: 'bg-slate-100 dark:bg-slate-700/50 text-slate-500 dark:text-slate-400',
        value: 'text-theme-text',
    },
    success: {
        icon: 'bg-emerald-500/10 dark:bg-emerald-500/20 text-emerald-600 dark:text-emerald-300',
        value: 'text-emerald-600 dark:text-emerald-300',
    },
    warning: {
        icon: 'bg-amber-500/10 dark:bg-amber-500/20 text-amber-600 dark:text-amber-300',
        value: 'text-amber-600 dark:text-amber-300',
    },
    danger: {
        icon: 'bg-red-500/10 dark:bg-red-500/20 text-red-600 dark:text-red-300',
        value: 'text-red-600 dark:text-red-300',
    },
    info: {
        icon: 'bg-sky-500/10 dark:bg-sky-500/20 text-sky-600 dark:text-sky-300',
        value: 'text-sky-600 dark:text-sky-300',
    },
};

// KPI / stat tile. Supersedes common/Card and the hand-rolled Results Store
// summary cards. Pass onClick + active to use it as a filter toggle. Dark mode
// uses the translucent well-lit "glass" surface (matches old common/Card
// brightness while letting the page background bleed through).
export function StatCard({ icon, title, value, details, onClick, active = false, className, children, compact = false, aside, tone = 'neutral' }) {
    const Root = onClick ? 'button' : 'div';
    const toneStyles = TONES[tone] || TONES.neutral;
    return (
        <Root
            onClick={onClick}
            type={onClick ? 'button' : undefined}
            className={cn(
                'bg-theme-card dark:bg-slate-800/60 border border-theme-border dark:border-slate-700/60',
                'backdrop-blur-xl rounded-xl shadow-lg flex items-start text-left transition-all',
                compact ? 'p-4 gap-3' : 'p-6 gap-4',
                onClick &&
                    'cursor-pointer hover:border-slate-400 dark:hover:border-slate-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50',
                active && 'border-emerald-500/50 ring-1 ring-emerald-500/40',
                className
            )}
        >
            {icon && (
                <div className={cn('rounded-lg shrink-0', toneStyles.icon, compact ? 'p-2' : 'p-3 mt-1')}>{icon}</div>
            )}
            <div className="flex-1 min-w-0">
                <p className={cn('text-slate-500 dark:text-slate-400 font-medium', compact ? 'text-xs mb-0.5' : 'text-sm mb-1')}>{title}</p>
                <h3 className={cn('font-bold', toneStyles.value, compact ? 'text-lg mb-1' : 'text-2xl mb-2')}>{value}</h3>
                {details && (
                    <div className="space-y-1">
                        {details.map((detail, idx) => (
                            <div key={idx} className="text-xs text-slate-500 dark:text-slate-400 flex justify-between gap-4">
                                <span>{detail.label}:</span>
                                <span className="font-mono text-slate-800 dark:text-slate-200">{detail.value}</span>
                            </div>
                        ))}
                    </div>
                )}
                {children}
            </div>
            {aside && <div className="shrink-0 self-center">{aside}</div>}
        </Root>
    );
}
