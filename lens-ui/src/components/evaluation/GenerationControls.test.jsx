import test from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ResourceBasisControl from './ResourceBasisControl.jsx';
import Tree from './OptimizationComparisonTree.jsx';

test('generation has one main button that opens resource confirmation', () => {
 const html=renderToStaticMarkup(createElement(ResourceBasisControl,{value:'available',hardware:{availableGpuCount:2,gpuCount:8},onGenerate(){}}));
 assert.match(html,/Generate YAML/);
 assert.equal((html.match(/<button/g)||[]).length,1);
 assert.match(html,/aria-haspopup="dialog"/);
 assert.doesNotMatch(html,/aria-label="YAML resource budget"/);
});
test('multiple branches remain active simultaneously and use a single selection entry',()=>{
 const html=renderToStaticMarkup(createElement(Tree,{guide:'optimized-baseline',selected:['full','affinity-only','load-only'],onChange(){}}));
 assert.equal((html.match(/type="checkbox"/g)||[]).length,0);
 assert.equal((html.match(/aria-pressed="true"/g)||[]).length,3);
 assert.doesNotMatch(html,/Add comparison scenario/);
 assert.match(html,/3 scenarios/);
 assert.match(html,/same workload/);
});

test('configuration card comparison summary contains only text, without diagrams or editing controls',async()=>{
 const {default:Summary}=await import('./OptimizationSelectionSummary.jsx');
 const html=renderToStaticMarkup(createElement(Summary,{guide:'optimized-baseline',selected:['full','load-only']}));
 assert.match(html,/2 comparison targets/);assert.match(html,/Full Guide/);assert.match(html,/Load-aware/);
 assert.doesNotMatch(html,/<svg|<button|<input/);
});
