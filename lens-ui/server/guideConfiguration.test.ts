import assert from 'node:assert/strict';
import test from 'node:test';
import { planDocuments } from './guidePlanning.ts';

const machine = { cpu: { logicalCores: 128 }, memoryGiB: 512, accelerator: { count: 16, memoryGiB: 24 } };
function fixture(args: string[], command = ['vllm', 'serve']) {
    return [
        { kind: 'ResourceClaimTemplate', metadata: { name: 'decode-claim' }, spec: { spec: { devices: { requests: [
            { name: 'gpu', exactly: { deviceClassName: 'gpu.intel.com', count: 1 } },
            { name: 'nic', exactly: { deviceClassName: 'dranet-rdma', count: 1, selectors: [{ cel: { expression: 'device.attributes["dra.net"].rdma == true' } }] } },
        ] } } } },
        { kind: 'Deployment', metadata: { name: 'decode' }, spec: { replicas: 1, template: { spec: { containers: [{ name: 'modelserver', command, args }] } } } },
    ];
}
const modelserver = (result) => result.documents[1].spec.template.spec.containers[0];
const plan = (documents, request = {}) => planDocuments(documents, 'New/Model', machine, null, { guide: 'optimized-baseline', replicas: 1, tensorParallelSize: 2, ...request });

test('TP updates remove separated and aliased old values', () => {
    for (const name of ['tensor-parallel-size', 'tensor_parallel_size', 'tp_size']) {
        const result = plan(fixture(['Old/Model', `--${name}`, '1', '--port=8000']));
        assert.deepEqual(result.validation.errors, []);
        assert.deepEqual(modelserver(result).args, ['New/Model', '--port=8000', '--tensor-parallel-size=2']);
    }
});

test('TP missing from a multiline shell invocation is added to the executed script', () => {
    const result = plan(fixture(['exec vllm serve Old/Model \\\n  --port 8000'], ['bash', '-c']));
    assert.deepEqual(result.validation.errors, []);
    assert.equal(modelserver(result).args.length, 1);
    assert.match(modelserver(result).args[0], /--tensor-parallel-size=2/);
});

test('precise model changes update structured KV events without corrupting the endpoint', () => {
    const result = plan(fixture(['Old/Model', '--kv-events-config', JSON.stringify({ publisher: 'zmq', endpoint: 'tcp://$(POD_IP):5557', topic: 'kv@$(POD_IP):8000@Old/Model' })]), { guide: 'precise-prefix-cache-routing' });
    assert.deepEqual(result.validation.errors, []);
    const args = modelserver(result).args;
    const config = JSON.parse(args[args.indexOf('--kv-events-config') + 1]);
    assert.equal(config.topic, 'kv@$(POD_IP):8000@New/Model');
    assert.equal(config.endpoint, 'tcp://$(POD_IP):5557');
});

test('RDMA NIC count and selectors remain independent from GPU TP and allowlist', () => {
    const previous = process.env.PRISM_GPU_PCI_ALLOWLIST;
    process.env.PRISM_GPU_PCI_ALLOWLIST = '0000:2b:00.0,0000:2f:00.0';
    try {
        const documents = fixture(['Old/Model']);
        const originalNic = structuredClone(documents[0].spec.spec!.devices.requests[1]);
        const result = plan(documents);
        const requests = result.documents[0].spec.spec.devices.requests;
        assert.equal(requests[0].exactly.count, 2);
        assert.deepEqual(requests[1], originalNic);
    } finally {
        if (previous === undefined) delete process.env.PRISM_GPU_PCI_ALLOWLIST;
        else process.env.PRISM_GPU_PCI_ALLOWLIST = previous;
    }
});

test('explicit RDMA NIC count applies only to network requests', () => {
    const result = plan(fixture(['Old/Model']), { guide: 'pd-disaggregation', guideVariant: 'vllm-rdma', decodeTensorParallelSize: 1, guideSettings: { rdmaNicCount: 2 } });
    const requests = result.documents[0].spec.spec.devices.requests;
    assert.equal(requests[0].exactly.count, 1);
    assert.equal(requests[1].exactly.count, 2);
});

test('native CPU cache capacity merges into the connector JSON', () => {
    const result = plan(fixture(['Old/Model', '--kv-transfer-config', '{"kv_connector":"OffloadingConnector","kv_role":"kv_both","kv_connector_extra_config":{"cpu_bytes_to_use":1,"other":true}}']), {
        guide: 'tiered-prefix-cache', guideVariant: 'native/cpu/base', guideSettings: { cacheCpuGiB: 2 },
    });
    assert.deepEqual(result.validation.errors, []);
    const argument = modelserver(result).args.find(item => item.startsWith('--kv-transfer-config='));
    const value = JSON.parse(argument.slice('--kv-transfer-config='.length));
    assert.deepEqual(value.kv_connector_extra_config, { cpu_bytes_to_use: 2147483648, other: true });
});

test('native CPU cache capacity reaches a connector value after a shell continuation', () => {
    const script = ['exec vllm serve Old/Model', '--kv-transfer-config', "'{\"kv_connector\":\"OffloadingConnector\",\"kv_connector_extra_config\":{\"cpu_bytes_to_use\":1}}'"].join(' \\\n');
    const result = plan(fixture([script], ['bash', '-c']), { guide: 'tiered-prefix-cache', guideVariant: 'native/cpu/base', guideSettings: { cacheCpuGiB: 2 } });
    assert.deepEqual(result.validation.errors, []);
    assert.match(modelserver(result).args[0], /2147483648/);
});

test('shared model directory keeps the logical API model identity', () => {
    const result = plan(fixture(['Old/Model']), { modelSource: 'shared-path', modelPath: '/data/model' });
    assert.ok(modelserver(result).args.includes('--served-model-name=New/Model'));
});

test('custom precise KV topic cannot override model identity', () => {
    const result = plan(fixture(['Old/Model', '--kv-events-config={"topic":"kv@host:8000@Old/Model"}']), { guide: 'precise-prefix-cache-routing', customParameters: [{ target: 'both', kind: 'argument', name: 'kv-events-config', value: '{"topic":"kv@host:8000@Wrong/Model"}' }] });
    assert.ok(result.validation.errors.some(error => /topic.*model/i.test(error)));
});

test('unknown entrypoints cannot be rewritten as if their first argument were a model', () => {
    const result = plan(fixture(['server-config.json'], ['python', 'wrapper.py']));
    assert.ok(result.validation.errors.some(error => /entrypoint/.test(error)));
    assert.deepEqual(modelserver(result).args, ['server-config.json']);
});

test('precise custom topic must keep the KV publisher prefix as well as model identity', () => {
    const result = plan(fixture(['Old/Model', '--kv-events-config={"topic":"kv@host:8000@Old/Model"}']), { guide: 'precise-prefix-cache-routing', customParameters: [{ target: 'both', kind: 'argument', name: 'kv-events-config', value: '{"topic":"oops@host:8000@New/Model"}' }] });
    assert.ok(result.validation.errors.some(error => /topic/.test(error)));
});
