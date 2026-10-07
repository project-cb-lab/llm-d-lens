import assert from 'node:assert/strict';
import test from 'node:test';
import * as configuration from './configuration.js';

test('explicit P:D pairs drive the same expanded output including TP and GPU budget', () => {
    const rows = configuration.expandConfigurationTopologies({ guide: 'pd-disaggregation', replicaVariants: '99', tensorParallelVariants: '1,2', prefillReplicaVariants: '99', prefillTensorParallelVariants: '1', pdTopologyVariants: '1:1,1:3' });
    assert.deepEqual(rows.map(row => [row.prefillReplicaCount, row.replicaCount, row.tpCount, row.gpuCount]), [[1,1,1,2],[1,1,2,3],[1,3,1,4],[1,3,2,7]]);
});
test('topology parsing rejects malformed, nonpositive and over-limit expanded sweeps', () => {
    const base = { guide: 'pd-disaggregation', replicaVariants: '1', tensorParallelVariants: '1,2,3,4', prefillReplicaVariants: '1', prefillTensorParallelVariants: '1,2,3,4' };
    for (const pdTopologyVariants of ['0:1', '1:x', '1:1,1:2']) assert.throws(() => configuration.expandConfigurationTopologies({ ...base, pdTopologyVariants }));
    assert.throws(() => configuration.expandConfigurationTopologies({ ...base, guide: 'optimized-baseline', tensorParallelVariants: '1,bad' }));
});
test('loading custom rows and changing one preserves the other saved overrides', () => {
    const rows = configuration.configurationCustomRows({ customParameters: [
        { target: 'both', kind: 'argument', name: 'block-size', value: '64' },
        { target: 'prefill', kind: 'environment', name: 'UCX_MEMTYPE_CACHE', value: 'n' },
        { target: 'decode', kind: 'argument', name: 'download-dir', value: '/cache' },
    ] });
    assert.equal(rows.length, 2);
    const next = rows.map((row, index) => index === 1 ? { ...row, value: '/new' } : row);
    assert.deepEqual(next[0], { target: 'prefill', kind: 'environment', name: 'UCX_MEMTYPE_CACHE', value: 'n' });
    assert.equal(configuration.configurationCustomRows({}).length, 0);
});

test('comparison draft inherits runtime controls but uses target Guide variant and fresh manifest', () => {
    const source = { deployable_configuration: { content: {
        model: { name: 'model' }, decode: { replicaCount: 2, tensorParallelSize: 4, maxModelLen: 8192 },
        guideVariant: 'vllm-rdma', officialGuide: { renderedManifest: 'old' },
        runtime: { image: 'same', storageVolumeId: 'volume' },
        customParameters: [{ target: 'both', kind: 'environment', name: 'FLAG', value: '1' }],
    } } };
    const draft = configuration.comparisonConfigurationDraft(source, { id: 'tiered-prefix-cache', evaluation: { default_variant: 'base' }, variants: ['base'] });
    assert.equal(draft.deployable_configuration.content.guideVariant, 'base');
    assert.equal(draft.deployable_configuration.content.officialGuide, undefined);
    assert.deepEqual(draft.deployable_configuration.content.decode, source.deployable_configuration.content.decode);
    assert.equal(draft.deployable_configuration.content.runtime.storageVolumeId, 'volume');
    draft.deployable_configuration.content.decode.replicaCount = 3;
    assert.equal(source.deployable_configuration.content.decode.replicaCount, 2);
});

test('comparison draft carries the original hardware selection forward instead of dropping it with officialGuide', () => {
    const source = { deployable_configuration: { content: {
        model: { name: 'model' }, decode: { replicaCount: 1 },
        officialGuide: { source: { accelerator: 'gpu' }, renderedManifest: 'old' },
    } } };
    const draft = configuration.comparisonConfigurationDraft(source, { id: 'precise-prefix-cache-routing', evaluation: { default_variant: 'base' }, variants: ['base'] });
    assert.equal(draft.deployable_configuration.content.officialGuide, undefined);
    assert.equal(draft.deployable_configuration.content.accelerator, 'gpu');
});

test('single-pool comparison inherits decode controls and omits prefill-only overrides', () => {
    const draft = configuration.comparisonConfigurationDraft({ deployable_configuration: { content: { customParameters: [
        { target: 'prefill', kind: 'environment', name: 'PREFILL_ONLY', value: '1' },
        { target: 'decode', kind: 'environment', name: 'DECODE_ONLY', value: '2' },
    ] } } }, { id: 'optimized-baseline' });
    assert.deepEqual(draft.deployable_configuration.content.customParameters.map(row => row.name), ['DECODE_ONLY']);
});

test('blank runtime controls inherit source values and explicit values are serialized', async () => {
    const { runtimeControlOverrides } = await import('./configuration.js');
    assert.deepEqual(runtimeControlOverrides({ blockSize: '', maxNumBatchedTokens: '', maxModelLen: '', maxNumSeqs: '', gpuMemoryUtilization: '' }, 'vllm'), []);
    assert.deepEqual(runtimeControlOverrides({ blockSize: '32' }, 'vllm'), [{ target: 'both', kind: 'argument', name: 'block-size', value: '32' }]);
    assert.deepEqual(runtimeControlOverrides({ blockSize: '32' }, 'sglang'), []);
});

test('role-specific tuning stays in the editable overrides on reload', () => {
    const parameter = { target: 'prefill', kind: 'argument', name: 'max-num-seqs', value: '128' };
    assert.deepEqual(configuration.configurationCustomRows({ customParameters: [parameter] }), [parameter]);
});

test('historical decode tuning facts do not become conflicting shared overrides on reload', async () => {
    const { configurationRuntimeValues } = await import('./configuration.js');
    const content = { decode: { maxNumSeqs: 64, maxModelLen: 8192 }, customParameters: [
        { target: 'decode', kind: 'argument', name: 'max-num-seqs', value: '64' },
        { target: 'prefill', kind: 'argument', name: 'max-model-len', value: '4096' },
    ] };
    assert.equal(configurationRuntimeValues(content).maxNumSeqs, '');
    assert.equal(configurationRuntimeValues(content).maxModelLen, '');
});

test('non-vLLM native custom arguments are preserved when reopening a configuration', () => {
    const parameter = { target: 'both', kind: 'argument', name: 'max-num-seqs', value: '128' };
    assert.deepEqual(configuration.configurationCustomRows({ runtime: { modelServer: 'sglang' }, customParameters: [parameter] }), [parameter]);
});

test('edited YAML keeps provenance and recalculates manifest and configuration checksums', async () => {
    const { editedEvaluationConfiguration } = await import('./configuration.js');
    const { configurationChecksum, sha256 } = await import('./checksum.js');
    const artifact = {artifact_id: 'original', deployable_configuration: {provider_ref: 'optimized-baseline', content: {model: {name: 'model'}, officialGuide: {variant: 'base'}}, provenance: {cluster_ref: {id: 'cluster'}}}};
    const before = structuredClone(artifact);
    const result = await editedEvaluationConfiguration(artifact, 'kind: Service\n', {now: () => 'edited-time'});
    assert.deepEqual(artifact, before);
    assert.equal(result.content.officialGuide.renderedManifest, 'kind: Service\n');
    assert.equal(result.content.officialGuide.manifestChecksum, await sha256('kind: Service\n'));
    assert.equal(result.checksum, await configurationChecksum(result.content));
    assert.equal(result.provenance.edited_from_artifact_id, 'original');
    assert.equal(result.provenance.edited_at, 'edited-time');
    assert.deepEqual(result.provenance.cluster_ref, {id: 'cluster'});
});
