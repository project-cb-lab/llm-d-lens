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

export function StatisticsPanel({ title, values }) {
    const rows = metricDistribution(values);
    return (
        <Panel title={title}>
            {rows.length ? (
                <dl className="divide-y divide-slate-800">
                    {rows.map(([label, value]) => (
                        <div key={label} className="flex items-center justify-between py-2 text-xs">
                            <dt className="text-slate-500">{label}</dt>
                            <dd className="font-mono font-semibold text-slate-200">{formatMilliseconds(value)}</dd>
                        </div>
                    ))}
                </dl>
            ) : <p className="text-xs text-slate-500">No statistics are available yet.</p>}
        </Panel>
    );
}
