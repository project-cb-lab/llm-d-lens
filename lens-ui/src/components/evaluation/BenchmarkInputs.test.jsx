import assert from 'node:assert/strict';
import test from 'node:test';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import BenchmarkInputs from './BenchmarkInputs.jsx';
import {applyBenchmarkPreset} from '../../features/evaluation/benchmarkSettings.js';

test('renders the submitted matrix, explicit SLO and all load stages without scenario defaults', () => {
 const b=applyBenchmarkPreset('throughput',{});
 const html=renderToStaticMarkup(createElement(BenchmarkInputs,{benchmark:b,setBenchmark(){},workloadMode:'matrix',selectWorkloadMode(){},slaTargets:{success_rate_min_percent:98,ttft_ms:450,ttft_percentile:'p95',tpot_ms:'',tpot_percentile:'p99'},setSlaTargets(){},targetCount:2,selectedArtifacts:[],recommendedBenchmark:b}));
 for (const [index, concurrency] of [1, 4, 8].entries()) {
  assert.match(html, new RegExp(`aria-label="Concurrency ${index + 1}"[^>]*value="${concurrency}"`));
 }
 assert.doesNotMatch(html, /Concurrency 4/);
 assert.match(html,/value="450"/);
 assert.match(html,/Minimum success rate/);
 assert.match(html,/6 measured combinations/);
 assert.match(html,/<details/);
 assert.doesNotMatch(html,/Default input tokens|Interactive chat|Traffic spike|Hide matrix/);
});

test('rate sweep keeps every stage visible and explains single-point limits', () => {
 const b={parallelism:1,wait_timeout_seconds:7200,shared_prefix:{num_groups:2,num_prompts_per_group:4,system_prompt_len:128,question_len:32,output_len:16,stages:[{rate:1,duration:30}]}};
 const html=renderToStaticMarkup(createElement(BenchmarkInputs,{benchmark:b,setBenchmark(){},workloadMode:'shared-prefix',selectWorkloadMode(){},slaTargets:{success_rate_min_percent:99,ttft_percentile:'p99',tpot_percentile:'p99'},setSlaTargets(){},targetCount:1,selectedArtifacts:[],recommendedBenchmark:b}));
 assert.match(html,/One load point/);
 assert.match(html,/30 seconds/);
 assert.doesNotMatch(html,/unique prefix tokens|Sample count defines/);
 assert.doesNotMatch(html,/Warm-up requests|Runner: inference-perf|Pause between stages|Poisson interval/);
});

function inputTree(overrides = {}) {
 let tree;
 const props = {benchmark:applyBenchmarkPreset('long'),setBenchmark(){},workloadMode:'matrix',selectWorkloadMode(){},slaTargets:{},setSlaTargets(){},targetCount:1,selectedArtifacts:[],...overrides};
 function Capture() { tree = BenchmarkInputs(props); return tree; }
 renderToStaticMarkup(createElement(Capture));
 return tree;
}
function findElement(tree, predicate) {
 if (!tree || typeof tree !== 'object') return undefined;
 if (predicate(tree)) return tree;
 return [tree.props?.children].flat(Infinity).map(child => findElement(child,predicate)).find(Boolean);
}



test('reference selection belongs to Configuration, not Benchmark inputs', () => {
 const tree=inputTree();
 assert.doesNotMatch(renderToStaticMarkup(tree), /Optional reference targets|EPP only/);
});



test('existing endpoints explain where to enable references without offering unsupported controls', () => {
 const tree=inputTree({targetMode:'existing',availableBaselines:['load-only']});
 assert.equal(findElement(tree,node=>node.type==='input' && node.props['aria-label']==='load-only'),undefined);
 assert.doesNotMatch(renderToStaticMarkup(tree),/>Configurations<|inherited from Configuration/);
});



test('benchmark starts with scenario controls without repeating configuration information',()=>{
 const html=renderToStaticMarkup(inputTree({summary:'This configuration summary must not appear'}));
 assert.doesNotMatch(html,/>Configurations<|This configuration summary must not appear|inherited from Configuration/);
 assert.match(html,/Benchmark settings/);
 assert.match(html,/>01 · Request workload</);
});


test('workload type is the only primary selector; custom sources share one entry', () => {
 const html = renderToStaticMarkup(inputTree());
 assert.doesNotMatch(html, /Guide defaults|Quick check|Concurrency sweep|Long inputs|role="tab"|Repository profile \(advanced\)/);
 assert.match(html, /Customize/);
 let mode;
 const tree = inputTree({workloadMode:'profile', selectWorkloadMode(value) { mode=value; }});
 findElement(tree, node => node.type==='button' && node.props.children==='Customize').props.onClick();
 assert.equal(mode, 'profile');
 findElement(tree, node => node.type==='select' && node.props['aria-label']==='YAML source').props.onChange({target:{value:'yaml'}});
 assert.equal(mode, 'yaml');
});

test('header add actions and row remove actions update the benchmark', () => {
 let benchmark = applyBenchmarkPreset('quick');
 const tree = inputTree({benchmark, setBenchmark(update) { benchmark=update(benchmark); }});
 const workload = findElement(tree, node => node.props?.title==='01 · Request workload');
 workload.props.action.props.onClick();
 assert.equal(benchmark.matrix.length, 2);
 const stages = findElement(tree, node => node.props?.title==='02 · Load stages');
 stages.props.action.props.onClick();
 assert.equal(benchmark.concurrency_stages.length, 2);
 const next = inputTree({benchmark, setBenchmark(update) { benchmark=update(benchmark); }});
 findElement(next, node => node.props?.['aria-label']==='Remove length 2').props.onClick();
 assert.equal(benchmark.matrix.length, 1);
 findElement(next, node => node.props?.['aria-label']==='Remove concurrency stage 2').props.onClick();
 assert.equal(benchmark.concurrency_stages.length, 1);
});


test('prefix reuse is offered only for a relevant guide or an active imported workload', () => {
 const generic = inputTree({recommendedBenchmark:applyBenchmarkPreset('quick')});
 assert.equal(findElement(generic, node => node.type==='button' && node.props.children==='Prefix cache reuse'), undefined);
 const recommendedBenchmark = {shared_prefix:{num_groups:2}};
 const prefixGuide = inputTree({recommendedBenchmark});
 assert.ok(findElement(prefixGuide, node => node.type==='button' && node.props.children==='Prefix cache reuse'));
 const imported = inputTree({workloadMode:'shared-prefix'});
 assert.ok(findElement(imported, node => node.type==='button' && node.props.children==='Prefix cache reuse'));
});

test('Customize opens the YAML editor from a fixed-length workload', () => {
 let mode;
 const tree = inputTree({selectWorkloadMode(value) { mode=value; }});
 findElement(tree, node => node.type==='button' && node.props.children==='Customize').props.onClick();
 assert.equal(mode, 'yaml');
 const yaml = inputTree({workloadMode:mode, benchmark:{workload_yaml:'load: {}'}});
 assert.ok(findElement(yaml, node => node.type==='textarea' && node.props['aria-label']==='Workload YAML'));
});


test('editing harness memory preserves the displayed workload before customization', () => {
 const benchmark = applyBenchmarkPreset('quick');
 let saved = null;
 const tree = inputTree({benchmark, setBenchmark(update) { saved = update(saved); }});
 const field = findElement(tree, node => node.props?.label === 'Load generator memory (GiB)');
 assert.equal(field.props.value, 8);
 field.props.onChange(64);
 assert.equal(saved.harness_memory_gib, 64);
 assert.deepEqual(saved.matrix, benchmark.matrix);
 assert.deepEqual(saved.concurrency_stages, benchmark.concurrency_stages);
 assert.equal(saved.wait_timeout_seconds, benchmark.wait_timeout_seconds);
});
