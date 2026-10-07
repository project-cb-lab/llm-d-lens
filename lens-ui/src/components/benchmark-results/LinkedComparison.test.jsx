import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup as render } from 'react-dom/server';
import LinkedComparison from './LinkedComparison.jsx';
const point = {id:'a',caseId:'a',loadKind:'rate',load:2,isl:null,osl:null,metrics:{throughput_tps:0}};

test('uses measured load when input length is missing, retaining zero throughput', () => {
    const html = render(<LinkedComparison runs={[point]} showMeasurements={false}/>);
    assert.match(html, /all-condition-chart/);
    assert.match(html, /value="load" selected/);
});

test('hides empty chart while retaining measurement rows', () => {
    const html = render(<LinkedComparison runs={[{...point,metrics:{}}]}/>);
    assert.doesNotMatch(html, /all-condition-chart|Load &amp; input length comparison|Comparison curves/);
    assert.match(html, /Results by configuration and load/);
});

test('renders nothing when no axes are recorded and measurements are disabled', () => {
    assert.equal(render(<LinkedComparison runs={[{...point,load:null}]} showMeasurements={false}/>), '');
    assert.equal(render(<LinkedComparison runs={[]} showMeasurements={false}/>), '');
});
