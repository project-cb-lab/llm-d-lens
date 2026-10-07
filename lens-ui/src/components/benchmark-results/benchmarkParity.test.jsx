import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup as render } from 'react-dom/server';
import { benchmarkDetails } from './benchmarkDetails.js';
import { buildExplorerRuns, resolveExplorerGuide } from './resultExplorer.js';
import { liveDeployments } from './deploymentEvidence.js';
import GuideResultExplorer from './GuideResultExplorer.jsx';

const run = {
    id: 'same-run', kind: 'guide', status: 'succeeded', model: 'test-model',
    guide: 'pd-disaggregation', deployment_ownership: 'existing-endpoint',
    stdout: 'Model: test-model\nRequest & Token Latency\n',
    configuration: { model: 'test-model', guide: 'pd-disaggregation', replicas: 2 },
    benchmark: { parallelism: 1, workload: 'sanity_random.yaml' },
    resource_snapshot: { pods: [{ name: 'model' }] },
    deployment_cases: [{ id: 'case', execution_id: 'execution', execution_status: 'ready' }],
    metrics: {
        throughput_tps: 120, success_rate: 100,
        observability: {
            status: 'available',
            window: { start: '2026-09-16T00:00:00Z', end: '2026-09-16T00:01:00Z' },
            series: [{ timestamp: '2026-09-16T00:00:00Z', queue_depth: 0 }],
        },
    },
};

test('standalone adapter retains configuration, telemetry, resource and live execution evidence', () => {
    const details = benchmarkDetails(run);
    assert.equal(details.workflow.deployment_ownership, 'existing-endpoint');
    assert.equal(resolveExplorerGuide(details), 'pd-disaggregation');
    assert.equal(liveDeployments(details.cases[0].deployment_cases)[0].liveAvailable, true);
    const owned = { ...details.workflow, deployment_ownership: 'evaluation' };
    const workflow = { workflow: owned, cases: [{ case: owned, evaluation: owned, deployment_cases: run.deployment_cases }] };
    assert.deepEqual(buildExplorerRuns(details), buildExplorerRuns(workflow));
});

test('existing endpoint logs never suppress the common structured result panel', () => {
    const standalone = benchmarkDetails(run);
    const owned = { ...standalone, workflow: { ...standalone.workflow, deployment_ownership: 'evaluation' } };
    const html = render(<GuideResultExplorer details={standalone} guideType="pd-disaggregation" />);
    assert.match(html, /Download benchmark files/);
    assert.match(html, /Compare/);
    assert.doesNotMatch(html, /Benchmark measurements extracted from Harness output/);
    assert.equal(html, render(<GuideResultExplorer details={owned} guideType="pd-disaggregation" />));
});

test('standalone resources show the same saved snapshot as workflow resources', () => {
    const standalone = benchmarkDetails(run);
    const owned = { ...standalone, workflow: { ...standalone.workflow, deployment_ownership: 'evaluation' } };
    const html = render(<GuideResultExplorer details={standalone} view="resources" guideType="pd-disaggregation" />);
    assert.match(html, /Download snapshot JSON/);
    assert.equal(html, render(<GuideResultExplorer details={owned} view="resources" guideType="pd-disaggregation" />));
});

test('legacy log metadata remains readable without inventing resource or monitoring evidence', () => {
    const details = benchmarkDetails({ id: 'legacy', stdout: 'Model: legacy-model\nNamespace: legacy-ns', metrics: {} });
    assert.equal(details.workflow.model, 'legacy-model');
    assert.equal(details.workflow.namespace, 'legacy-ns');
    assert.deepEqual(details.cases[0].deployment_cases, []);
    assert.equal(buildExplorerRuns(details)[0].resources, null);
});