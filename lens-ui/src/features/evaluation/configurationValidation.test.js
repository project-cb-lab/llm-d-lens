import assert from 'node:assert/strict';
import test from 'node:test';
import { validateConfigurationInputs } from './configurationValidation.js';

const base = { guide: 'optimized-baseline', modelServer: 'vllm', guideVariant: '', guideVariants: [], guideSettings: {}, customRows: [], replicaVariants: '1', tensorParallelVariants: '1', prefillReplicaVariants: '1', prefillTensorParallelVariants: '1', pdTopologyVariants: '', runtime: {} };
const validate = (patch) => validateConfigurationInputs({ ...base, ...patch });

test('empty custom name produces one field error; an empty argument value is legal', () => {
    const errors = validate({ customRows: [{ target: 'both', kind: 'argument', name: '', value: '' }] });
    assert.equal(errors.length, 1);
    assert.equal(errors[0].field, 'custom.0.name');
    assert.deepEqual(validate({ customRows: [{ target: 'both', kind: 'argument', name: 'enable-prefix-caching', value: '' }] }), []);
});
test('overlap marks both conflicting rows but allows separate P/D roles', () => {
    const rows = [{ target: 'both', kind: 'argument', name: 'max-model-len', value: '100' }, { target: 'decode', kind: 'argument', name: 'max-model-len', value: '200' }];
    assert.deepEqual(validate({ guide: 'pd-disaggregation', guideVariant: 'vllm', customRows: rows }).map(e => e.field).sort(), ['custom.0.name', 'custom.1.name']);
    rows[0].target = 'prefill';
    assert.deepEqual(validate({ guide: 'pd-disaggregation', guideVariant: 'vllm', customRows: rows }), []);
});
test('shared runtime conflict identifies the shared control and custom row', () => {
    const errors = validate({ runtime: { maxModelLen: '100' }, customRows: [{ target: 'both', kind: 'argument', name: 'max-model-len', value: '200' }] });
    assert.deepEqual(errors.map(e => e.field).sort(), ['custom.0.name', 'maxModelLen']);
});
test('invalid YAML and guide-specific values target their own fields', () => {
    const errors = validate({ guideSettings: { routerValues: 'router: [', cacheCpuGiB: '4', rdmaNicCount: '0' } });
    assert.deepEqual(errors.map(e => e.field).sort(), ['cacheCpuGiB', 'rdmaNicCount', 'routerValues']);
    assert.equal(validate({ guideSettings: { routerValues: '[]' } })[0].field, 'routerValues');
});
test('topology syntax errors identify topology controls', () => {
    assert.equal(validate({ tensorParallelVariants: '1,' })[0].field, 'tensorParallelVariants');
    assert.equal(validate({ guide: 'pd-disaggregation', guideVariant: 'vllm', pdTopologyVariants: '1:' })[0].field, 'pdTopologyVariants');
});
test('runtime bounds and unsupported prefill are field errors', () => {
    const errors = validate({ runtime: { maxNumSeqs: '0', gpuMemoryUtilization: '1.2' }, customRows: [{ target: 'prefill', kind: 'environment', name: 'BAD-NAME', value: '' }] });
    assert.deepEqual(errors.map(e => e.field).sort(), ['custom.0.name', 'custom.0.target', 'gpuMemoryUtilization', 'maxNumSeqs']);
});

test('administrator-managed Hugging Face environment variables are rejected', () => {
    for (const name of ['HF_HOME', 'HF_TOKEN', 'TRANSFORMERS_OFFLINE', 'HUGGINGFACE_HUB_CACHE']) {
        const errors = validate({ customRows: [{ target: 'both', kind: 'environment', name, value: 'x' }] });
        assert.equal(errors.length, 1, name);
        assert.equal(errors[0].field, 'custom.0.name');
        assert.match(errors[0].message, /managed by the platform administrator/);
    }
    assert.deepEqual(validate({ customRows: [{ target: 'both', kind: 'environment', name: 'VLLM_LOGGING_LEVEL', value: 'DEBUG' }] }), []);
});

test('server parameter errors map to active fields; network errors stay global', async () => {
    const { configurationServerErrors } = await import('./configurationValidation.js');
    const mapped = configurationServerErrors(['Precise token-load routing currently requires one EPP replica.', 'Requested topology needs 32 accelerators, but only 8 are available to Prism.', 'Connection refused'], { sweepEnabled: true, pdTopologyVariants: '1:2' });
    assert.deepEqual(mapped.map(error => error.field), ['routerValues', 'pdTopologyVariants']);
});

for (const availableAccelerators of [0, 1, 8]) {
    test(`GPU shortage (${availableAccelerators} available) does not block YAML input validation`, () => {
        assert.deepEqual(validate({ replicaVariants: '3', tensorParallelVariants: '4', availableAccelerators }), []);
        assert.deepEqual(validate({ guide: 'pd-disaggregation', guideVariant: 'vllm', pdTopologyVariants: '1:2', tensorParallelVariants: '4', prefillTensorParallelVariants: '4', availableAccelerators }), []);
    });
}
