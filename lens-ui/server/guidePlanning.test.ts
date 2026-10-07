import assert from 'node:assert/strict';
import test from 'node:test';

import { ROUTER_DISAGG_SIDECAR_IMAGE } from './guideDeploymentBundle.ts';
import { applyCustomPatches, isAcceleratorDeviceClass, planDocuments, resolveAcceleratorVariant, runtimeClassForAccelerator, validateManifestCapacity } from './guidePlanning.ts';

test('the vendor container runtime is used when the cluster defines one', () => {
    assert.equal(runtimeClassForAccelerator({ runtimeClasses: [{ name: 'nvidia', handler: 'nvidia' }] }, 'gpu'), 'nvidia');
    assert.equal(runtimeClassForAccelerator({ runtimeClasses: [{ name: 'intel-gpu', handler: 'intel' }] }, 'xpu'), 'intel-gpu');
    assert.equal(runtimeClassForAccelerator({ runtimeClasses: [{ name: 'nvidia', handler: 'nvidia' }] }, 'xpu'), null);
    assert.equal(runtimeClassForAccelerator({ runtimeClasses: [] }, 'gpu'), null);
    assert.equal(runtimeClassForAccelerator(null, 'gpu'), null);
});

test('the guide variant follows the cluster hardware across both access modes', () => {
    // NVIDIA, extended-resource only (A100: no DRA device classes) -> gpu.
    assert.equal(resolveAcceleratorVariant('xpu', { allocatable: { accelerators: { 'nvidia.com/gpu': 7 } }, deviceClasses: [] }), 'gpu');
    // NVIDIA via DRA.
    assert.equal(resolveAcceleratorVariant('xpu', { allocatable: { accelerators: {} }, deviceClasses: ['gpu.nvidia.com'] }), 'gpu');
    // Intel via extended resource or DRA -> xpu.
    assert.equal(resolveAcceleratorVariant('gpu', { allocatable: { accelerators: { 'gpu.intel.com/i915': 32 } }, deviceClasses: [] }), 'xpu');
    assert.equal(resolveAcceleratorVariant('gpu', { allocatable: { accelerators: {} }, deviceClasses: ['gpu.intel.com'] }), 'xpu');
    // Unknown cluster keeps the requested variant; no cluster keeps it too.
    assert.equal(resolveAcceleratorVariant('xpu', { allocatable: { accelerators: {} }, deviceClasses: [] }), 'xpu');
    assert.equal(resolveAcceleratorVariant('xpu', null), 'xpu');
});

test('cluster-discovered device classes count as accelerators without a regex change', () => {
    assert.equal(isAcceleratorDeviceClass('accel.example.com', ['accel.example.com']), true);
    assert.equal(isAcceleratorDeviceClass('accel.example.com', []), false);
    assert.equal(isAcceleratorDeviceClass('gpu.nvidia.com', []), true);
    assert.equal(isAcceleratorDeviceClass('dranet-rdma', []), false);
    assert.equal(isAcceleratorDeviceClass('', ['accel.example.com']), false);
});

function deployment(
    role: 'prefill' | 'decode',
    replicas: number,
    tp: number,
    env: Array<Record<string, unknown>> = [],
) {
    return {
        apiVersion: 'apps/v1',
        kind: 'Deployment',
        metadata: { name: `pd-${role}`, labels: { 'llm-d.ai/role': role } },
        spec: {
            replicas,
            template: {
                spec: {
                    containers: [{
                        name: 'modelserver',
                        image: 'ghcr.io/llm-d/llm-d-xpu:v0.8.0',
                        args: ['Qwen/Qwen3-0.6B', `--tensor-parallel-size=${tp}`],
                        resources: { limits: { 'gpu.intel.com/i915': tp } },
                        env,
                    }],
                    initContainers: role === 'decode' ? [{
                        name: 'routing-proxy',
                        image: 'ghcr.io/llm-d/llm-d-router-disagg-sidecar:main',
                        imagePullPolicy: 'Always',
                        args: ['--port=8000', '--kv-connector=nixlv2'],
                    }] : [],
                },
            },
        },
    };
}

function claim(role: 'prefill' | 'decode', count: number) {
    return {
        apiVersion: 'resource.k8s.io/v1',
        kind: 'ResourceClaimTemplate',
        metadata: { name: `pd-${role}-claim`, labels: { 'llm-d.ai/role': role } },
        spec: { spec: { devices: { requests: [{ exactly: { deviceClassName: 'gpu.intel.com', count } }] } } },
    };
}

test('PD planning reports prefill and decode tensor parallel sizes independently', () => {
    const documents = [
        deployment('decode', 3, 1),
        deployment('prefill', 1, 1),
        claim('decode', 1),
        claim('prefill', 1),
    ];
    const machine = { cpu: { logicalCores: 128 }, memoryGiB: 512, accelerator: { model: 'B60', count: 4 } };
    const cluster = {
        accelerator: { model: 'B60', count: 4, devices: [] },
        allocatable: { cpu: 128, memoryGiB: 512, accelerators: {} },
        deviceClasses: ['gpu.intel.com'],
        preflightPassed: true,
    };

    const result = planDocuments(documents, 'Qwen/Qwen3-32B', machine, cluster, {
        guide: 'pd-disaggregation',
        prefillReplicas: 1,
        prefillTensorParallelSize: 1,
        decodeReplicas: 1,
        decodeTensorParallelSize: 4,
        runtimeImage: 'ghcr.io/llm-d/llm-d-xpu:v0.9.0',
    });

    assert.deepEqual(result.summary.tensorParallelSizeByRole, { decode: 4, prefill: 1 });
    assert.deepEqual(result.summary.plannedReplicasByRole, { decode: 1, prefill: 1 });
    assert.equal(result.summary.requestedAccelerators, 5);
    assert.ok(result.validation.errors.some((message: string) => message.startsWith('Prefill TP=1 is too small')));
    assert.ok(!result.validation.errors.some((message: string) => message.startsWith('Decode TP=4 is too small')));
    assert.ok(!result.validation.warnings.some((message: string) => message.includes('manifest requests 4')));
    const decode = result.documents.find((item) => item.metadata?.name === 'pd-decode');
    assert.equal(decode.spec.template.spec.containers[0].image, 'ghcr.io/llm-d/llm-d-xpu:v0.9.0');
    assert.equal(decode.spec.template.spec.initContainers[0].image, ROUTER_DISAGG_SIDECAR_IMAGE);
    assert.equal(decode.spec.template.spec.initContainers[0].imagePullPolicy, 'IfNotPresent');
    assert.ok(decode.spec.template.spec.initContainers[0].args.includes('--vllm-port=8200'));
});

test('planning uses node-scoped allowlist when DRA PCI inventory is temporarily unavailable', () => {
    const previousAllowlist = process.env.PRISM_GPU_PCI_ALLOWLIST;
    const previousTarget = process.env.PRISM_K8S_TARGET_NODE;
    process.env.PRISM_GPU_PCI_ALLOWLIST = '0000:2b:00.0,0000:2f:00.0';
    process.env.PRISM_K8S_TARGET_NODE = 'smc-16';
    try {
        const result = planDocuments(
            [deployment('decode', 1, 1), claim('decode', 1)],
            'Qwen/Qwen3-8B',
            { cpu: { logicalCores: 128 }, memoryGiB: 512, accelerator: { model: 'B60', count: 8 } },
            {
                nodes: [{ name: 'smc-16' }],
                accelerator: { model: 'B60', count: 8, devices: [] },
                allocatable: { cpu: 128, memoryGiB: 512, accelerators: {} },
                deviceClasses: ['gpu.intel.com'], preflightPassed: true,
            },
            { guide: 'optimized-baseline', replicas: 1, tensorParallelSize: 1 },
        );
        assert.ok(!result.validation.errors.some((message: string) => message.includes('PCI address')));
        assert.ok(result.validation.warnings.some((message: string) => message.includes('did not return DRA PCI-address inventory')));
        assert.equal(result.summary.availableAccelerators, 2);
    } finally {
        if (previousAllowlist === undefined) delete process.env.PRISM_GPU_PCI_ALLOWLIST;
        else process.env.PRISM_GPU_PCI_ALLOWLIST = previousAllowlist;
        if (previousTarget === undefined) delete process.env.PRISM_K8S_TARGET_NODE;
        else process.env.PRISM_K8S_TARGET_NODE = previousTarget;
    }
});

test('planning rejects a real PCI mismatch when DRA inventory is available', () => {
    const previousAllowlist = process.env.PRISM_GPU_PCI_ALLOWLIST;
    const previousTarget = process.env.PRISM_K8S_TARGET_NODE;
    process.env.PRISM_GPU_PCI_ALLOWLIST = '0000:2b:00.0,0000:2f:00.0';
    process.env.PRISM_K8S_TARGET_NODE = 'smc-16';
    try {
        const result = planDocuments(
            [deployment('decode', 1, 1), claim('decode', 1)],
            'Qwen/Qwen3-8B',
            { cpu: { logicalCores: 128 }, memoryGiB: 512, accelerator: { model: 'B60', count: 1 } },
            {
                nodes: [{ name: 'smc-16' }],
                accelerator: { model: 'B60', count: 1, devices: [{ pciAddress: '0000:2B:00.0', driver: 'gpu.intel.com' }] },
                allocatable: { cpu: 128, memoryGiB: 512, accelerators: {} },
                deviceClasses: ['gpu.intel.com'], preflightPassed: true,
            },
            { guide: 'optimized-baseline', replicas: 1, tensorParallelSize: 1 },
        );
        assert.ok(result.validation.errors.some((message: string) => message.includes('0000:2f:00.0')));
        assert.ok(!result.validation.errors.some((message: string) => message.includes('0000:2b:00.0')));
    } finally {
        if (previousAllowlist === undefined) delete process.env.PRISM_GPU_PCI_ALLOWLIST;
        else process.env.PRISM_GPU_PCI_ALLOWLIST = previousAllowlist;
        if (previousTarget === undefined) delete process.env.PRISM_K8S_TARGET_NODE;
        else process.env.PRISM_K8S_TARGET_NODE = previousTarget;
    }
});

test('planning makes the Hugging Face token optional for public models', () => {
    const modelDeployment = deployment('decode', 1, 1, [{
        name: 'HF_TOKEN',
        valueFrom: { secretKeyRef: { name: 'llm-d-hf-token', key: 'HF_TOKEN' } },
    }]);

    const result = planDocuments(
        [modelDeployment, claim('decode', 1)],
        'Qwen/Qwen3-0.6B',
        { cpu: { logicalCores: 128 }, memoryGiB: 512, accelerator: { model: 'B60', count: 1 } },
        {
            accelerator: { model: 'B60', count: 1, devices: [] },
            allocatable: { cpu: 128, memoryGiB: 512, accelerators: {} },
            deviceClasses: ['gpu.intel.com'],
            preflightPassed: true,
        },
        { guide: 'optimized-baseline', replicas: 1, tensorParallelSize: 1 },
    );

    const secretKeyRef = result.documents[0].spec.template.spec.containers[0].env[0].valueFrom.secretKeyRef;
    assert.equal(secretKeyRef.optional, true);
});

test('auto-cache planning runs the model server with Hugging Face offline', () => {
    const result = planDocuments(
        [deployment('decode', 1, 1), claim('decode', 1)],
        'Qwen/Qwen3-0.6B',
        { cpu: { logicalCores: 128 }, memoryGiB: 512, accelerator: { model: 'B60', count: 1 } },
        {
            accelerator: { model: 'B60', count: 1, devices: [] },
            allocatable: { cpu: 128, memoryGiB: 512, accelerators: {} },
            deviceClasses: ['gpu.intel.com'],
            preflightPassed: true,
        },
        {
            guide: 'optimized-baseline', replicas: 1, tensorParallelSize: 1,
            modelSource: 'auto-cache', modelPvcClaimName: 'model-cache-pvc',
        },
    );

    const env = result.documents[0].spec.template.spec.containers[0].env;
    const byName = Object.fromEntries(env.map((item: { name: string; value: string }) => [item.name, item.value]));
    assert.equal(byName.HF_HOME, '/model-cache');
    assert.equal(byName.HF_HUB_OFFLINE, '1');
    assert.equal(byName.TRANSFORMERS_OFFLINE, '1');
    assert.equal(byName.HF_HUB_DISABLE_XET, '1');
});

test('planning rejects user-supplied Hugging Face environment overrides', () => {
    const result = planDocuments(
        [deployment('decode', 1, 1), claim('decode', 1)],
        'Qwen/Qwen3-0.6B',
        { cpu: { logicalCores: 128 }, memoryGiB: 512, accelerator: { model: 'B60', count: 1 } },
        null,
        { guide: 'optimized-baseline', replicas: 1, tensorParallelSize: 1, customParameters: [{ target: 'both', kind: 'environment', name: 'HF_HOME', value: '/models' }] },
    );
    assert.ok(result.validation.errors.some((message: string) => message.includes('platform administrator')));
});

function claimWithNamedRequests(name: string, requests: Array<{ name: string; count: number }>) {
    return {
        apiVersion: 'resource.k8s.io/v1',
        kind: 'ResourceClaimTemplate',
        metadata: { name },
        spec: { spec: { devices: { requests: requests.map((request) => ({ name: request.name, exactly: { deviceClassName: 'gpu.intel.com', count: request.count } })) } } },
    };
}

test('applyCustomPatches merges an uploaded patch into the matching resource by kind+name', () => {
    const documents = [claimWithNamedRequests('pd-decode-claim', [{ name: 'xpu', count: 4 }])];
    const errors: string[] = [];
    const warnings: string[] = [];

    const patchYaml = `
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: pd-decode-claim
spec:
  spec:
    devices:
      requests:
        - name: rdma-nic
          exactly:
            deviceClassName: rdma.intel.com
            count: 2
`;
    const applied = applyCustomPatches(documents, [{ path: 'patch-rdma.yaml', content: patchYaml }], errors, warnings);

    assert.deepEqual(errors, []);
    assert.equal(applied.length, 1);
    assert.equal(applied[0].action, 'merged');
    const requests = documents[0].spec.spec.devices.requests;
    assert.equal(requests.length, 2);
    assert.deepEqual(requests.find((request) => request.name === 'xpu')?.exactly, { deviceClassName: 'gpu.intel.com', count: 4 });
    assert.deepEqual(requests.find((request) => request.name === 'rdma-nic')?.exactly, { deviceClassName: 'rdma.intel.com', count: 2 });
});

test('applyCustomPatches appends a new resource with a warning when nothing matches', () => {
    const documents: unknown[] = [claimWithNamedRequests('pd-decode-claim', [{ name: 'xpu', count: 4 }])];
    const errors: string[] = [];
    const warnings: string[] = [];

    const patchYaml = `
apiVersion: v1
kind: ConfigMap
metadata:
  name: extra-config
data:
  foo: bar
`;
    const applied = applyCustomPatches(documents, [{ path: 'extra.yaml', content: patchYaml }], errors, warnings);

    assert.deepEqual(errors, []);
    assert.equal(applied.length, 1);
    assert.equal(applied[0].action, 'appended');
    assert.ok(warnings.some((message) => message.includes('did not match an existing resource')));
    assert.equal(documents.length, 2);
    assert.equal((documents[1] as { metadata: { name: string } }).metadata.name, 'extra-config');
});

test('applyCustomPatches reports a clear error for an ambiguous unnamed patch target', () => {
    const documents = [
        claimWithNamedRequests('pd-decode-claim', [{ name: 'xpu', count: 4 }]),
        claimWithNamedRequests('pd-prefill-claim', [{ name: 'xpu', count: 4 }]),
    ];
    const errors: string[] = [];
    const warnings: string[] = [];

    const patchYaml = `
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
spec:
  spec:
    devices:
      requests:
        - name: rdma-nic
          exactly:
            deviceClassName: rdma.intel.com
            count: 2
`;
    const applied = applyCustomPatches(documents, [{ path: 'ambiguous.yaml', content: patchYaml }], errors, warnings);

    assert.equal(applied.length, 0);
    assert.ok(errors.some((message) => message.includes('without metadata.name')));
    // Neither claim was mutated.
    assert.equal(documents[0].spec.spec.devices.requests.length, 1);
    assert.equal(documents[1].spec.spec.devices.requests.length, 1);
});

test('applyCustomPatches is a no-op that changes nothing when no patches are provided', () => {
    const documents = [claimWithNamedRequests('pd-decode-claim', [{ name: 'xpu', count: 4 }])];
    const snapshot = JSON.stringify(documents);
    const errors: string[] = [];
    const warnings: string[] = [];

    const applied = applyCustomPatches(documents, [], errors, warnings);

    assert.deepEqual(applied, []);
    assert.deepEqual(errors, []);
    assert.deepEqual(warnings, []);
    assert.equal(JSON.stringify(documents), snapshot);
});


test('runtime overrides reach both argv and shell model servers with role targeting', () => {
    const prefill = deployment('prefill', 1, 1);
    const decode = deployment('decode', 1, 1);
    const server = decode.spec.template.spec.containers[0];
    Object.assign(server, { command: ['bash', '-c'], args: ['exec vllm serve Qwen/Qwen3-0.6B --block-size=16'] });
    const result = planDocuments([prefill, decode], 'Qwen/Qwen3-0.6B', { cpu: { logicalCores: 128 }, memoryGiB: 512, accelerator: { count: 8 } }, null, {
        guide: 'pd-disaggregation', modelServer: 'vllm', blockSize: 64, maxNumBatchedTokens: 4096, maxModelLen: 8192, maxNumSeqs: 32, gpuMemoryUtilization: 0.85,
        customParameters: [
            { target: 'prefill', kind: 'environment', name: 'UCX_MEMTYPE_CACHE', value: 'n' },
            { target: 'decode', kind: 'argument', name: 'download-dir', value: 'literal $(touch /tmp/never-run)' },
        ],
    });
    assert.deepEqual(result.validation.errors, []);
    assert.ok(prefill.spec.template.spec.containers[0].args.includes('--block-size=64'));
    assert.ok(prefill.spec.template.spec.containers[0].args.includes('--max-num-batched-tokens=4096'));
    assert.ok(prefill.spec.template.spec.containers[0].env.some(item => item.name === 'UCX_MEMTYPE_CACHE' && item.value === 'n'));
    assert.match(server.args[0], /--block-size=64/);
    assert.match(server.args[0], /--max-num-batched-tokens=4096/);
    for (const flag of ['--max-model-len=8192', '--max-num-seqs=32', '--gpu-memory-utilization=0.85']) {
        assert.ok(server.args[0].includes(flag));
        assert.ok(prefill.spec.template.spec.containers[0].args.includes(flag));
    }
    assert.ok(!server.args[0].includes("\n+"));
    assert.ok(server.args[0].includes("--download-dir='literal $(touch /tmp/never-run)'"));
    assert.ok(!server.env.some(item => item.name === 'UCX_MEMTYPE_CACHE'));
});

test('explicit replica counts cannot be silently reduced while artifact facts retain the requested count', () => {
    const result = planDocuments([deployment('decode', 1, 1)], 'Qwen/Qwen3-0.6B', { cpu: { logicalCores: 0 }, memoryGiB: 0 }, null, { replicas: 2, tensorParallelSize: 1 });
    // Give the deployment a concrete CPU request so capacity actually constrains it.
    const constrained = deployment('decode', 1, 1);
    Object.assign(constrained.spec.template.spec.containers[0].resources.limits, { cpu: '2', memory: '1Gi' });
    const planned = planDocuments([constrained], 'Qwen/Qwen3-0.6B', { cpu: { logicalCores: 2 }, memoryGiB: 8 }, null, { replicas: 2, tensorParallelSize: 1 });
    assert.ok(planned.validation.errors.some(message => message.includes('replicas')));
    assert.ok(result.summary);
});

test('blank advanced controls preserve guide arguments and unrelated configuration', () => {
    for (const modelServer of ['vllm', 'sglang']) {
        const document = deployment('decode', 1, 1, [{ name: 'SOURCE_SETTING', value: 'keep' }]);
        document.spec.template.spec.containers[0].args.push('--source-option=unchanged', '--max-num-seqs=77');
        const result = planDocuments([document], 'Qwen/Qwen3-0.6B', { cpu: { logicalCores: 128 }, memoryGiB: 512, accelerator: { count: 8 } }, null, {
            guide: 'optimized-baseline', modelServer, maxNumSeqs: '', blockSize: '',
        });
        assert.deepEqual(result.validation.errors, []);
        const container = result.documents[0].spec.template.spec.containers[0];
        assert.ok(container.args.includes('--source-option=unchanged'));
        assert.ok(container.args.includes('--max-num-seqs=77'));
        assert.ok(!container.args.some(arg => arg.startsWith('--block-size')));
        assert.ok(container.env.some(item => item.name === 'SOURCE_SETTING' && item.value === 'keep'));
    }
});

test('role-specific tuning is applied only to the requested model server', () => {
    const documents = [deployment('prefill', 1, 1), deployment('decode', 1, 1)];
    const result = planDocuments(documents, 'Qwen/Qwen3-0.6B', { cpu: { logicalCores: 128 }, memoryGiB: 512, accelerator: { count: 8 } }, null, {
        guide: 'pd-disaggregation', modelServer: 'vllm', customParameters: [
            { target: 'prefill', kind: 'argument', name: 'max-num-seqs', value: '128' },
            { target: 'decode', kind: 'argument', name: 'max-num-seqs', value: '32' },
        ],
    });
    assert.deepEqual(result.validation.errors, []);
    assert.ok(documents[0].spec.template.spec.containers[0].args.includes('--max-num-seqs=128'));
    assert.ok(documents[1].spec.template.spec.containers[0].args.includes('--max-num-seqs=32'));
});

for (const available of [0, 1, 8]) {
    for (const guide of ['optimized-baseline', 'pd-disaggregation']) {
        test(`${guide} rejects insufficient capacity without silently shrinking requested topology with only ${available} GPUs`, () => {
            const pd = guide === 'pd-disaggregation';
            const documents = [deployment('decode', 1, 1), claim('decode', 1)];
            if (pd) documents.push(deployment('prefill', 1, 1), claim('prefill', 1));
            const result = planDocuments(documents, 'Qwen/Qwen3-0.6B', {
                cpu: { logicalCores: 128 }, memoryGiB: 512,
                accelerator: { model: 'B60', count: available },
            }, null, pd ? {
                guide, decodeReplicas: 2, decodeTensorParallelSize: 4,
                prefillReplicas: 1, prefillTensorParallelSize: 4,
            } : { guide, replicas: 3, tensorParallelSize: 4 });
            assert.ok(result.validation.errors.some(message => message.includes('Insufficient resources')));
            assert.equal(result.validation.status, 'invalid');
            assert.equal(result.summary.requestedAccelerators, 12);
            const decode = result.documents.find(item => item.metadata?.name === 'pd-decode');
            assert.equal(decode.spec.replicas, pd ? 2 : 3);
            assert.ok(decode.spec.template.spec.containers[0].args.includes('--tensor-parallel-size=4'));
            assert.equal(result.documents.find(item => item.metadata?.name === 'pd-decode-claim').spec.spec.devices.requests[0].exactly.count, 4);
            if (pd) assert.equal(result.documents.find(item => item.metadata?.name === 'pd-prefill').spec.replicas, 1);
        });
    }
}

test('recommendation basis preserves zero availability and uses total cards when selected', () => {
    const machine = { accelerator: { count: 8, memoryGiB: 24 }, cpu: { logicalCores: 32 }, memoryGiB: 128 };
    const request = { replicas: 2, tensorParallelSize: 1, recommendationGpuCount: 0 };
    const available = planDocuments([deployment('decode', 2, 1)], 'Qwen/Qwen3-0.6B', machine, null, request);
    const total = planDocuments([deployment('decode', 2, 1)], 'Qwen/Qwen3-0.6B', machine, null, {...request, resourceBasis: 'total'});
    assert.equal(available.summary.availableAccelerators, 0);
    assert.equal(available.summary.maxRecommendedReplicas, 0);
    assert.equal(total.summary.availableAccelerators, 8);
    assert.equal(total.summary.maxRecommendedReplicas, 8);
    assert.equal(available.summary.plannedReplicas, 2);
    assert.ok(available.validation.errors.some(message => message.includes('Insufficient resources')));
    assert.equal(total.validation.errors.length, 0);
});


test('zero budget rejects inherited topology when replicas and TP are omitted', () => {
    const result = planDocuments([deployment('decode', 1, 1)], 'Qwen/Qwen3-0.6B', { accelerator: { count: 8 } }, null, { recommendationGpuCount: 0 });
    assert.equal(result.summary.requestedAccelerators, 1);
    assert.ok(result.validation.errors.some(error => error.startsWith('Insufficient resources:')));
});

test('final capacity validation catches custom patches increasing replicas', () => {
    const documents = [deployment('decode', 1, 1)];
    const errors: string[] = [];
    applyCustomPatches(documents, [{ path: 'replicas.yaml', content: 'kind: Deployment\nmetadata:\n  name: pd-decode\nspec:\n  replicas: 9\n' }], errors, []);
    assert.equal(validateManifestCapacity(documents, 1, 'available', errors), 9);
    assert.ok(errors.some(error => error.includes('requires 9 accelerators')));
});

test('DRA demand counts all attached claims across replicas and both serving roles', () => {
    const decode = deployment('decode', 2, 1);
    const decodePodSpec = decode.spec.template.spec as unknown as {
        containers: Array<{ resources?: unknown }>;
        resourceClaims?: Array<{ resourceClaimTemplateName: string }>;
    };
    delete decodePodSpec.containers[0].resources;
    decodePodSpec.resourceClaims = [{ resourceClaimTemplateName: 'pd-decode-claim' }];
    const prefill = deployment('prefill', 1, 1);
    const documents = [decode, prefill, claim('decode', 4)];
    const errors: string[] = [];
    assert.equal(validateManifestCapacity(documents, 8, 'total', errors), 9);
    assert.equal(errors.length, 1);
    validateManifestCapacity(documents, 9, 'total', errors);
    assert.deepEqual(errors, []);
});


test('GPU init containers and restartable sidecars count toward peak pod capacity', () => {
    const document = deployment('decode', 1, 1);
    const podSpec = document.spec.template.spec as unknown as {
        initContainers?: Array<{
            name: string;
            resources: { requests: Record<string, number> };
            restartPolicy?: string;
        }>;
    };
    podSpec.initContainers = [{ name: 'prepare', resources: { requests: { 'nvidia.com/gpu': 4 } } }];
    const errors: string[] = [];
    assert.equal(validateManifestCapacity([document], 1, 'available', errors), 4);
    assert.equal(errors.length, 1);
    podSpec.initContainers[0].restartPolicy = 'Always';
    assert.equal(validateManifestCapacity([document], 4, 'available', errors), 5);
    assert.equal(errors.length, 1);
});
