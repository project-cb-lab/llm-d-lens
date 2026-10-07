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
import { Panel } from '../ui';
import { formatMilliseconds, metricDistribution } from '../../features/simulation/presentation';

export function MetricBars({ series, emptyMessage = 'No distribution data is available yet.' }) {
    const rows = series.flatMap(({ label, values, tone = 'bg-cyan-400' }) => (
        metricDistribution(values).map(([stat, value]) => ({ label: `${label} ${stat}`, value, tone }))
    ));
    const maximum = Math.max(...rows.map((row) => row.value), 0);
    if (!rows.length) return <p className="py-10 text-center text-xs text-slate-500">{emptyMessage}</p>;
    return (
        <div className="space-y-2.5">
            {rows.map((row) => (
                <div key={row.label} className="grid grid-cols-[7.5rem_1fr_auto] items-center gap-3 text-[11px]">
                    <span className="truncate text-slate-400">{row.label}</span>
                    <div className="h-2 overflow-hidden rounded-full bg-slate-800">
                        <div className={`h-full rounded-full ${row.tone}`} style={{ width: maximum > 0 ? `${Math.max(2, row.value / maximum * 100)}%` : '2%' }} />
                    </div>
                    <span className="min-w-16 text-right font-mono text-slate-300">{formatMilliseconds(row.value)}</span>
                </div>
            ))}
        </div>
    );
}
