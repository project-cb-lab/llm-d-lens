import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import yaml from 'js-yaml';
import ResultExports from './ResultExports.jsx';
import { buildResultExportState, resolveResultExport } from './resultExports.js';

const manifest = 'apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: recorded-model\n';
const fixture = () => ({ workflow: { id: 'workflow-a', runtime: { harness: 'inference-perf' }, configuration_artifacts: { 'saved-id': 'hash' } }, cases: [{ case: { id: 'candidate', configuration_artifact_id: 'saved-id', benchmark: { workload: 'shared-prefix', parallelism: 8, seed: 42 }, deployment_configuration: { provider_ref: 'pd-disaggregation', checksum: 'hash', content: { model: { name: 'model-a' }, officialGuide: { renderedManifest: manifest, deploymentBundle: { helm: { values: [{ name: 'router-effective.yaml', content: 'weights: 2' }] }, resources: [] } } } } }, deployment_cases: [{ resource_snapshot: { live: 'must-not-export' } }] }] });
const run = { id: 'candidate:stage-0', caseId: 'candidate', stage: true, scopeLabel: 'Stage client summary', metrics: { throughput_tps: 12 }, observability: {}, configuration: {}, caseObservability: {} };

test('model YAML is exactly the persisted text with no network or regeneration', async () => {
    const state = buildResultExportState(fixture(), run);
    const file = await resolveResultExport(state, 'manifest', { fetch: () => { throw new Error('Unexpected network'); } });
    assert.equal(await file.blob.text(), manifest);
    assert.match(file.filename, /modelserver\.yaml$/);
});

test('reproduction inputs retain selected case configuration, benchmark and artifact provenance', async () => {
    const state = buildResultExportState(fixture(), run);
    const file = await resolveResultExport(state, 'reproduction');
    const saved = JSON.parse(await file.blob.text());
    assert.equal(saved.benchmark.seed, 42);
    assert.equal(saved.deployment_configuration.content.officialGuide.renderedManifest, manifest);
    assert.equal(saved.configuration_artifact_id, 'saved-id');
    assert.equal(saved.selection.result_id, 'candidate:stage-0');
    assert.match(saved.reproduction_notes.join(' '), /calibration/i);
    assert.doesNotMatch(await file.blob.text(), /must-not-export/);
});

test('bundle download uses the selected immutable artifact route and surfaces missing bundle errors', async () => {
    const state = buildResultExportState(fixture(), run);
    const fetch = async (url) => {
        assert.equal(url, '/api/v1/configurations/artifacts/saved-id/bundle');
        return new Response('archive', { status: 200, headers: { 'Content-Type': 'application/zip' } });
    };
    const file = await resolveResultExport(state, 'bundle', { fetch });
    assert.equal(await file.blob.text(), 'archive');
    await assert.rejects(resolveResultExport(state, 'bundle', { fetch: async () => new Response(JSON.stringify({ detail: 'configuration artifact not found' }), { status: 404 }) }), /artifact not found/);
    await assert.rejects(resolveResultExport(state, 'bundle', { fetch: async () => new Response(JSON.stringify({ detail: 'This older configuration contains model-server YAML only.' }), { status: 409 }) }), /model-server YAML only/);
});

test('an unmatched selected case cannot borrow another case model or bundle artifact', async () => {
    const state = buildResultExportState(fixture(), { ...run, caseId: 'baseline', id: 'baseline' });
    assert.equal(state.manifest, null);
    assert.equal(state.artifactId, null);
    await assert.rejects(resolveResultExport(state, 'manifest'), /not saved/i);
    await assert.rejects(resolveResultExport(state, 'bundle'), /artifact/i);
});

test('download menu offers exactly the four benchmark artifacts', () => {
    const html = renderToStaticMarkup(<ResultExports details={fixture()} run={run} />);
    for (const label of ['Download benchmark files', 'Deployment ZIP', 'Benchmark inputs YAML', 'Performance targets YAML', 'Benchmark results CSV']) assert.ok(html.includes(label));
    assert.doesNotMatch(html, /Reproduction inputs JSON|Evidence JSON|Model-server YAML/);
});

test('YAML exports preserve saved benchmark inputs and performance targets', async () => {
    const details = fixture();
    details.cases[0].case.sla_targets = { ttft_ms: 500, success_rate_min_percent: 99 };
    const state = buildResultExportState(details, run);
    const inputs = await resolveResultExport(state, 'inputs');
    assert.deepEqual(yaml.load(await inputs.blob.text()), details.cases[0].case.benchmark);
    const targets = await resolveResultExport(state, 'targets');
    assert.deepEqual(yaml.load(await targets.blob.text()), details.cases[0].case.sla_targets);
    await assert.rejects(resolveResultExport(buildResultExportState(fixture(), run), 'targets'), /not recorded/i);
});

test('CSV includes every measured case and distinct stage dimensions and escapes names', async () => {
    const details = fixture();
    details.cases[0].case.name = 'Model, "A"';
    details.cases[0].case.matrix_results = [{ isl: 128, osl: 64, stage_metrics: [{ concurrency: 4, metrics: { throughput_tps: 0, latency_distributions: { ttft: { p95_ms: 12 } } } }] }];
    details.cases.push({ case: { id: 'baseline', metrics: { throughput_tps: 45 } } });
    const file = await resolveResultExport(buildResultExportState(details, run), 'results');
    const csv = await file.blob.text();
    assert.match(file.filename, /results.csv$/);
    assert.match(csv, /metrics.latency_distributions.ttft.p95_ms/);
    assert.match(csv, /"Model, ""A"""/);
    assert.match(csv, /baseline/);
    assert.match(csv, /128,64/);
    assert.equal(csv.trim().split('\r\n').length, 3);
});

test('reproduction keeps comparison arm, endpoint choice and predeclared SLA without inferring missing flags', async () => {
    const details = fixture();
    Object.assign(details.cases[0].case, { kind: 'baseline', baseline_type: 'kubernetes-service', endpoint_kind: 'kubernetes-service', baseline_parameters: { route: 'service' }, sla_targets: { ttft_ms: 500, tpot_ms: 50 }, dependent_guide_case_id: 'candidate-epp' });
    details.cases[0].evaluation = { use_baseline_endpoint: true, sla_targets: { ttft_ms: 500, tpot_ms: 50 }, endpoint: 'http://saved-service:8000', deployment_execution_id: 'execution-a' };
    const file = await resolveResultExport(buildResultExportState(details, run), 'reproduction');
    const saved = JSON.parse(await file.blob.text());
    assert.equal(saved.case.kind, 'baseline');
    assert.equal(saved.case.baseline_type, 'kubernetes-service');
    assert.equal(saved.case.endpoint_kind, 'kubernetes-service');
    assert.equal(saved.case.dependent_guide_case_id, 'candidate-epp');
    assert.equal(saved.case.sla_targets.ttft_ms, 500);
    assert.equal(saved.execution.use_baseline_endpoint, true);
    assert.equal(saved.execution.endpoint, 'http://saved-service:8000');
    assert.equal(saved.execution.sla_targets.tpot_ms, 50);
    assert.equal(Object.hasOwn(buildResultExportState(fixture(), run).reproduction.execution, 'use_baseline_endpoint'), false);
    details.cases[0].evaluation.use_baseline_endpoint = false;
    assert.equal(buildResultExportState(details, run).reproduction.execution.use_baseline_endpoint, false);
});
