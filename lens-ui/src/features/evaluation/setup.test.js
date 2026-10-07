import assert from 'node:assert/strict';
import test from 'node:test';
import { matchesEvaluationSetup, retainCompatibleConfigurations } from './setup.js';

const setup = {
    cluster: { id: 'cluster-a' }, model: 'Qwen/Qwen3-0.6B', modelServer: 'vllm',
    image: 'example/runtime:v1', imageMode: 'use-upstream-image', buildSourceUrl: '',
    modelSource: 'auto-cache', storageVolumeId: 'cache-a', modelPath: '/models',
};
const artifact = (id, overrides = {}) => ({
    artifact_id: id,
    deployable_configuration: {
        provenance: { cluster_ref: { id: 'cluster-a' } },
        content: { model: { name: setup.model }, runtime: {
            modelServer: 'vLLM', image: setup.image, modelSource: setup.modelSource,
            storageVolumeId: setup.storageVolumeId, ...overrides,
        } },
    },
});

test('back/continue retains all selected variants, without adding other saved artifacts', () => {
    const artifacts = [artifact('a'), artifact('b'), artifact('unselected')];
    assert.deepEqual(retainCompatibleConfigurations(artifacts, ['b', 'a'], setup), ['b', 'a']);
    assert.deepEqual(retainCompatibleConfigurations(artifacts, [], setup), []);
    assert.equal(matchesEvaluationSetup(artifacts[0], setup), true);
});

test('changed model, cluster, runtime, image, or cache invalidates an immutable saved configuration', () => {
    for (const change of [
        { model: 'Qwen/Qwen3-8B' }, { cluster: { id: 'cluster-b' } },
        { modelServer: 'vllm-rdma' }, { image: 'example/runtime:v2' },
        { storageVolumeId: 'cache-b' }, { modelSource: 'huggingface' },
        { imageMode: 'build-from-source', buildSourceUrl: 'https://example.org/runtime' },
    ]) {
        assert.equal(matchesEvaluationSetup(artifact('a'), { ...setup, ...change }), false, JSON.stringify(change));
    }
});

test('shared paths and image build sources must match while irrelevant cache fields are ignored', () => {
    const shared = { ...setup, modelSource: 'shared-path', imageMode: 'build-from-source', buildSourceUrl: 'https://example.org/runtime' };
    const saved = artifact('a', { modelSource: 'shared-path', mountPath: '/models', imageMode: shared.imageMode, buildSourceUrl: shared.buildSourceUrl });
    assert.equal(matchesEvaluationSetup(saved, shared), true);
    assert.equal(matchesEvaluationSetup(saved, { ...shared, storageVolumeId: 'unused' }), true);
    assert.equal(matchesEvaluationSetup(saved, { ...shared, modelPath: '/other' }), false);
    assert.equal(matchesEvaluationSetup(saved, { ...shared, buildSourceUrl: 'https://example.org/other' }), false);
});
