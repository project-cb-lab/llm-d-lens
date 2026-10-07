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
import { ChartTooltip, ChartTooltipRow } from '../ui/charts/ChartTooltip';
import { formatNumber, formatMilliseconds } from '../../features/simulation/presentation';

export function SimulationTooltip({ active, payload, label }) {
    if (!active || !payload?.length) return null;
    return <ChartTooltip title={`Elapsed ${formatNumber(label)}s`}>
        {payload.map((item) => <ChartTooltipRow key={item.dataKey} color={item.color} label={item.name}
            value={item.dataKey === 'request_arrival_rps' ? formatNumber(item.value, ' req/s') : formatMilliseconds(item.value)} />)}
    </ChartTooltip>;
}
