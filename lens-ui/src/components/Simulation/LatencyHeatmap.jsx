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
import { formatNumber, formatMilliseconds, formatCount } from '../../features/simulation/presentation';

const HEATMAP_COLORS = ['#a78bfa', '#60a5fa', '#34d399', '#fb7185'];

function heatmapColor(value) {
    const position = Math.max(0, Math.min(1, value)) * (HEATMAP_COLORS.length - 1);
    const startIndex = Math.min(Math.floor(position), HEATMAP_COLORS.length - 2);
    const progress = position - startIndex;
    const channels = HEATMAP_COLORS.slice(startIndex, startIndex + 2).map((color) => (
        [1, 3, 5].map((offset) => Number.parseInt(color.slice(offset, offset + 2), 16))
    ));
    return `rgb(${channels[0].map((channel, index) => (
        Math.round(channel + (channels[1][index] - channel) * progress)
    )).join(' ')})`;
}

export function LatencyHeatmap({ title, metric, data }) {
    const cells = Array.isArray(data?.cells) ? data.cells : [];
    if (!cells.length) {
        return (
            <Panel title={title}>
                <p className="py-12 text-center text-xs text-slate-500">
                    No per-request {metric} and sequence-length data are available.
                </p>
            </Panel>
        );
    }

    const width = 800;
    const height = 256;
    const margin = { top: 12, right: 24, bottom: 48, left: 76 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const maximumTime = Math.max(...cells.map((cell) => Number(cell.end_seconds)));
    const minimumSequence = Math.min(...cells.map((cell) => Number(cell.sequence_length_start)));
    const maximumSequence = Math.max(...cells.map((cell) => Number(cell.sequence_length_end)));
    const sequenceRange = Math.max(1, maximumSequence - minimumSequence);
    const minimumMetric = Number(data.minimum_metric_ms);
    const maximumMetric = Number(data.maximum_metric_ms);
    const metricRange = Math.max(0.001, maximumMetric - minimumMetric);
    const x = (value) => margin.left + (Number(value) / Math.max(0.001, maximumTime)) * plotWidth;
    const y = (value) => margin.top + ((maximumSequence - Number(value)) / sequenceRange) * plotHeight;
    const xTicks = Array.from({ length: 5 }, (_, index) => maximumTime * index / 4);
    const yTicks = Array.from({ length: 5 }, (_, index) => minimumSequence + sequenceRange * index / 4);
    const sequenceLabel = data.sequence_length === 'input'
        ? 'Input sequence length'
        : `${data.sequence_length_source === 'requested' ? 'Requested ' : ''}output sequence length`;

    return (
        <Panel title={title}>
            <div className="w-full overflow-x-auto">
                <svg viewBox={`0 0 ${width} ${height}`} className="h-64 min-w-[640px] w-full" role="img" aria-label={`${metric} heatmap by time and ${sequenceLabel.toLowerCase()}`}>
                    <rect x={margin.left} y={margin.top} width={plotWidth} height={plotHeight} fill="#020617" stroke="#334155" />
                    {xTicks.map((tick) => (
                        <g key={`x-${tick}`}>
                            <line x1={x(tick)} x2={x(tick)} y1={margin.top} y2={margin.top + plotHeight} stroke="#1e293b" strokeDasharray="3 3" />
                            <text x={x(tick)} y={height - 25} textAnchor="middle" fill="#64748b" fontSize="11">{formatNumber(tick)}s</text>
                        </g>
                    ))}
                    {yTicks.map((tick) => (
                        <g key={`y-${tick}`}>
                            <line x1={margin.left} x2={margin.left + plotWidth} y1={y(tick)} y2={y(tick)} stroke="#1e293b" strokeDasharray="3 3" />
                            <text x={margin.left - 9} y={y(tick) + 4} textAnchor="end" fill="#64748b" fontSize="11">{formatNumber(tick)}</text>
                        </g>
                    ))}
                    {cells.map((cell) => {
                        const normalized = (Number(cell.average_metric_ms) - minimumMetric) / metricRange;
                        const cellX = x(cell.start_seconds);
                        const cellY = y(cell.sequence_length_end);
                        const cellWidth = Math.max(1, x(cell.end_seconds) - cellX);
                        const cellHeight = Math.max(1, y(cell.sequence_length_start) - cellY);
                        return (
                            <rect
                                key={`${cell.start_seconds}-${cell.sequence_length_start}`}
                                x={cellX}
                                y={cellY}
                                width={cellWidth}
                                height={cellHeight}
                                fill={heatmapColor(normalized)}
                                stroke="#020617"
                                strokeWidth="0.5"
                            >
                                <title>
                                    {`Time ${formatNumber(cell.start_seconds)}–${formatNumber(cell.end_seconds)}s\n${sequenceLabel} ${formatNumber(cell.sequence_length_start)}–${formatNumber(cell.sequence_length_end)} tokens\nAverage ${metric} ${formatMilliseconds(cell.average_metric_ms)}\n${formatCount(cell.request_count)} requests`}
                                </title>
                            </rect>
                        );
                    })}
                    <text x={margin.left + plotWidth / 2} y={height - 5} textAnchor="middle" fill="#94a3b8" fontSize="11">Elapsed time</text>
                    <text transform={`translate(16 ${margin.top + plotHeight / 2}) rotate(-90)`} textAnchor="middle" fill="#94a3b8" fontSize="11">{sequenceLabel} (tokens)</text>
                </svg>
            </div>
            <div className="mt-2 flex items-center justify-end gap-2 text-[10px] text-slate-500">
                <span>{formatMilliseconds(minimumMetric)}</span>
                <span
                    className="h-2.5 w-28 rounded-sm"
                    style={{ background: `linear-gradient(90deg, ${HEATMAP_COLORS.join(', ')})` }}
                />
                <span>{formatMilliseconds(maximumMetric)}</span>
            </div>
        </Panel>
    );
}
