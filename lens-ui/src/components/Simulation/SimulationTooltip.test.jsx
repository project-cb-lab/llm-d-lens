import React from 'react';
import test from 'node:test';
import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { SimulationTooltip } from './SimulationTooltip';
import { LatencyMetricTimeline } from './LatencyMetricTimeline';

test('tooltip preserves mixed rate and latency units including zero', () => {
    const html = renderToStaticMarkup(<SimulationTooltip active label={0} payload={[
        { dataKey: 'request_arrival_rps', name: 'Request arrival rate', value: 0 },
        { dataKey: 'average_latency_ms', name: 'Average TTFT', value: 25 },
    ]} />);
    assert.match(html, /0 req\/s/);
    assert.match(html, /25 ms/);
});
test('empty latency data retains unavailable text', () => {
    assert.match(renderToStaticMarkup(<LatencyMetricTimeline title="Latency" metric="TTFT" data={[]} color="green" />), /No per-request TTFT timestamps/);
});
