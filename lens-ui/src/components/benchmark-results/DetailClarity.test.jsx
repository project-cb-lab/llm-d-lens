import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToStaticMarkup as render} from 'react-dom/server';
import GuideResultExplorer from './GuideResultExplorer.jsx';
import {AllCaseTelemetry} from './AllCaseMechanism.jsx';
import ResourceExplorer from './ResourceExplorer.jsx';
const details={cases:[{case:{id:'a',configuration:{model:'model',replicas:3,tensorParallelSize:2,benchmark:{parallelism:8}},metrics:{throughput_tps:120}}}]};
test('telemetry hides empty charts and chooses a recorded metric including zero',()=>{
 assert.equal(render(<AllCaseTelemetry runs={[{id:'a',caseId:'a'}]}/>),'');
 const html=render(<AllCaseTelemetry runs={[{id:'a',caseId:'a',observability:{window:{start:'2026-09-09T00:00:00Z',end:'2026-09-09T00:01:00Z'},series:[{timestamp:'2026-09-09T00:00:00Z',queue_depth:0}]}}]}/>);
 assert.match(html,/Waiting requests/);
 assert.doesNotMatch(html,/Prefix cache ratio|No saved/);
});
test('removed sections fall back to results with the download menu',()=>{
 for(const view of ['configuration','evidence','mechanism']) {
  const html=render(<GuideResultExplorer details={details} view={view}/>);
  assert.match(html,/Download benchmark files/);
  assert.doesNotMatch(html,/View configuration|Search evidence|Reference configuration/);
 }
});
test('saved snapshot has a download and absent snapshots add no empty section',()=>{
 assert.match(render(<ResourceExplorer run={{resources:{pods:[{name:'engine'}]}}}/>),/Download snapshot JSON/);
 assert.doesNotMatch(render(<ResourceExplorer run={{}}/>),/Deployment resource snapshot/);
});
