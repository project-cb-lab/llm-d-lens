import test from 'node:test';
import assert from 'node:assert/strict';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import Tree from './OptimizationComparisonTree.jsx';
import {toggleOptimizationSelection} from '../../features/evaluation/experimentDesign.js';
test('combination selection is independent and enforces the same-pod dependency',()=>{
 assert.deepEqual(toggleOptimizationSelection(['full'],'router-neutral'),['full','router-neutral']);
 assert.deepEqual(toggleOptimizationSelection([],'kubernetes-service'),['kubernetes-service','full']);
 assert.deepEqual(toggleOptimizationSelection(['kubernetes-service','full'],'full'),[]);
});
test('scenario tree exposes selectable branches without checkbox lists',()=>{
 const html=renderToStaticMarkup(createElement(Tree,{guide:'optimized-baseline',selected:['full'],onChange(){}}));
 assert.match(html,/Compare serving scenarios with the same workload/);
 assert.doesNotMatch(html,/Add comparison scenario/);
 assert.match(html,/Full Guide/);
 assert.match(html,/aria-label="Serving scenario comparison tree"/);
 assert.match(html,/aria-pressed="true"/);
 assert.doesNotMatch(html,/type="checkbox"/);

});
