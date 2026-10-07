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

export function ConfigurationGroup({ title, items }) {
    const visibleItems = items.filter(([, value]) => value !== null && value !== undefined && value !== '');
    if (!visibleItems.length) return null;
    return (
        <section className="rounded-lg border border-slate-800 bg-slate-950/35 p-4">
            <h4 className="mb-3 text-[10px] font-bold uppercase tracking-wider text-slate-500">{title}</h4>
            <dl className="grid gap-x-5 gap-y-3 text-xs sm:grid-cols-2">
                {visibleItems.map(([label, value]) => (
                    <div key={label} className={label === 'Trace path' || label === 'Endpoint' ? 'sm:col-span-2' : ''}>
                        <dt className="text-slate-600">{label}</dt>
                        <dd className="mt-0.5 break-all text-slate-200">{value}</dd>
                    </div>
                ))}
            </dl>
        </section>
    );
}
