import React from 'react';
import assert from 'node:assert/strict';
import test from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import HistoricalTimeRange from './HistoricalTimeRange.jsx';
import {historicalBounds, historicalPreset, parseHistoricalRange} from './historicalRange.js';
const data = [0, 60, 120, 180].map(elapsed => ({elapsed,timestamp:new Date(Date.UTC(2026,0,1)+elapsed*1000).toISOString()}));
test('presets anchor to last recorded timestamp, not wall clock',()=>{
 assert.deepEqual(historicalPreset(data,60),[120,180]);
 assert.deepEqual(historicalPreset(data,600),[0,180]);
 assert.equal(historicalBounds([]),null);
});
test('UTC interval validation rejects inversion and out-of-window ranges',()=>{
 assert.deepEqual(parseHistoricalRange(data,'2026-01-01T00:01:00','2026-01-01T00:02:00').range,[60,120]);
 assert.match(parseHistoricalRange(data,'2026-01-01T00:02:00','2026-01-01T00:01:00').error,/before/);
 assert.match(parseHistoricalRange(data,'2025-12-31T23:59:00','2026-01-01T00:02:00').error,/inside/);
 assert.match(parseHistoricalRange(data,'bad','bad').error,/UTC/);
});
test('time control makes historical scope and non-filtered summary metrics explicit',()=>{
 const html=renderToStaticMarkup(<HistoricalTimeRange data={data} range={[60,120]} onRange={()=>{}} onTimeMode={()=>{}} />);
 assert.doesNotMatch(html,/From \(UTC\)|To \(UTC\)/);assert.match(html,/Final 1 min/);
 assert.match(html,/2 \/ 4 timestamps/);assert.match(html,/not client percentiles/);
 assert.doesNotMatch(html,/Apply interval/);
 assert.equal(renderToStaticMarkup(<HistoricalTimeRange data={[]} onRange={()=>{}} />),'');
});
test('fractional timestamps survive applying the displayed full interval',()=>{
 const samples=[{elapsed:0,timestamp:'2026-01-01T00:00:00.500Z'},{elapsed:5,timestamp:'2026-01-01T00:00:05.500Z'}];
 assert.deepEqual(parseHistoricalRange(samples,'2026-01-01T00:00:00.500','2026-01-01T00:00:05.500').range,[0,5]);
 const html=renderToStaticMarkup(<HistoricalTimeRange data={samples} onRange={()=>{}} />);
 assert.match(html,/2 \/ 2 timestamps/);
});
