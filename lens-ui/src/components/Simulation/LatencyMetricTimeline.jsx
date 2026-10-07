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
import { ChartLegend } from '../ui/charts/ChartLegend';
import { SimulationTooltip } from './SimulationTooltip';
import { CHART_SERIES } from '../ui/charts/palette';
import { Panel } from '../ui';
import { formatNumber, formatMilliseconds } from '../../features/simulation/presentation';
import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

export function LatencyMetricTimeline({ title, metric, data, color }) {
    return (
        <Panel title={title}>
            {data.length > 0 ? (
                <>
                    <ChartLegend entries={[
                        { label: 'Request arrival rate', color: CHART_SERIES[0] },
                        { label: `Average ${metric}`, color },
                        { label: `P95 ${metric}`, color },
                    ]} className="mb-3" />
                    <div className="h-64 w-full">
                        <ResponsiveContainer width="100%" height="100%">
                            <ComposedChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                                <XAxis dataKey="time_seconds" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(value) => `${formatNumber(value)}s`} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" />
                                <YAxis yAxisId="pressure" width={52} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" label={{ value: 'req/s', angle: -90, position: 'insideLeft', fill: '#64748b', fontSize: 10 }} />
                                <YAxis yAxisId="metric" orientation="right" width={56} tick={{ fill: '#64748b', fontSize: 10 }} stroke="#334155" label={{ value: 'ms', angle: 90, position: 'insideRight', fill: '#64748b', fontSize: 10 }} />
                                <Tooltip content={<SimulationTooltip />} />
                                <Area yAxisId="pressure" type="monotone" dataKey="request_arrival_rps" name="Request arrival rate" stroke={CHART_SERIES[0]} strokeWidth={2} fill={CHART_SERIES[0]} fillOpacity={0.14} dot={false} isAnimationActive={false} />
                                <Area yAxisId="metric" type="monotone" dataKey="average_latency_ms" name={`Average ${metric}`} stroke={color} strokeWidth={2} fill={color} fillOpacity={0.14} connectNulls dot={false} isAnimationActive={false} />
                                <Line yAxisId="metric" type="monotone" dataKey="p95_latency_ms" name={`P95 ${metric}`} stroke={color} strokeWidth={2} strokeDasharray="5 3" connectNulls dot={false} isAnimationActive={false} />
                            </ComposedChart>
                        </ResponsiveContainer>
                    </div>
                </>
            ) : <p className="py-12 text-center text-xs text-slate-500">No per-request {metric} timestamps are available.</p>}
        </Panel>
    );
}
