import assert from 'node:assert/strict';
import test from 'node:test';
import { deploymentEvidence, liveDeployments } from './deploymentEvidence.js';

test('unavailable observability is never presented as monitoring ready', () => {
    const result = deploymentEvidence({ execution_status: 'ready', evaluationCase: { metrics: { observability: { status: 'unavailable', reason: 'Prometheus unreachable' } } } });
    assert.equal(result.telemetry, 'Unavailable');
    assert.equal(result.telemetryReason, 'Prometheus unreachable');
});

test('reads embedded model and serving config, preserving zero values', () => {
    const result = deploymentEvidence({ resource_snapshot: { pods: [], requested_gpus: 0 }, evaluationCase: { deployment_configuration: { content: { model: { name: 'model-a' }, decode: { replicaCount: 2, tensorParallelSize: 4 }, runtime: { image: 'vllm:test' } } } } });
    assert.equal(result.model, 'model-a');
    assert.equal(result.replicas, 2);
    assert.equal(result.tp, 4);
    assert.equal(result.gpus, 0);
    assert.equal(result.podCount, 0);
});

test('live deployment selection keeps execution identity and actual lifecycle separate from evaluation', () => {
    const deployments = liveDeployments([
        { id: 'a', execution_id: 'old', status: 'succeeded', execution_status: 'cleaned' },
        { id: 'b', execution_id: 'new', status: 'failed', execution_status: 'ready' },
        { id: 'c', execution_id: 'new', execution_status: 'ready' },
        { id: 'pending', pendingDeployment: true },
    ]);
    assert.equal(deployments.length, 2);
    assert.equal(deployments[0].liveAvailable, false);
    assert.equal(deployments[1].liveAvailable, true);
    assert.equal(deployments[1].execution_id, 'new');
});
