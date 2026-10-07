import { configurationChecksum, sha256 } from './checksum.js';

export const RUNTIME_CONTROL_ARGUMENTS = ['max-model-len', 'max-num-seqs', 'gpu-memory-utilization', 'block-size', 'max-num-batched-tokens'];

export function comparisonConfigurationDraft(source, provider) {
    const content = structuredClone(source.deployable_configuration.content);
    // The comparison arm renders through the legacy per-provider overlay (it
    // has no officialGuide/renderedManifest of its own), so the cluster's
    // accelerator must be carried forward explicitly rather than re-derived.
    const accelerator = content.officialGuide?.source?.accelerator || content.accelerator;
    delete content.officialGuide;
    // Guide-specific cache/network/router tuning must be reviewed for each comparison arm.
    delete content.guideSettings;
    if (accelerator) content.accelerator = accelerator;
    if (provider.id !== 'pd-disaggregation') {
        delete content.prefill;
        content.customParameters = (content.customParameters || content.custom_parameters || []).filter(item => item.target !== 'prefill');
        delete content.custom_parameters;
    }
    content.guideVariant = provider.evaluation?.default_variant || provider.variants?.[0] || '';
    return { deployable_configuration: { provider_ref: provider.id, content } };
}

export function configurationCustomRows(content = {}) {
    return (content.customParameters || content.custom_parameters || [])
        .filter(item => !((content.runtime?.modelServer || 'vllm') === 'vllm' && item.kind === 'argument' && item.target === 'both' && RUNTIME_CONTROL_ARGUMENTS.includes(item.name)))
        .map(item => ({ ...item, value: String(item.value) }));
}

export function expandConfigurationTopologies({ guide, replicaVariants, tensorParallelVariants, prefillReplicaVariants, prefillTensorParallelVariants, pdTopologyVariants = '' }) {
    const parse = (value, label) => {
        const parts = String(value).split(',').map(item => item.trim());
        const values = parts.map(Number);
        if (parts.some(item => !item) || values.some(item => !Number.isSafeInteger(item) || item < 1)) throw new Error(`${label} must contain positive integers.`);
        return [...new Set(values)];
    };
    const isPd = guide === 'pd-disaggregation';
    const tps = parse(tensorParallelVariants, 'TP variants');
    const prefillTps = isPd ? parse(prefillTensorParallelVariants, 'Prefill TP variants') : [0];
    let pairs;
    if (isPd && pdTopologyVariants.trim()) {
        pairs = [...new Set(pdTopologyVariants.split(',').map(item => item.trim()))].map(item => {
            if (!/^\d+\s*:\s*\d+$/.test(item)) throw new Error('Explicit P/D topologies require P:D pairs, such as 1:1,1:3.');
            const pair = item.split(':').map(Number);
            if (pair.some(value => !Number.isSafeInteger(value) || value < 1)) throw new Error('P/D replica counts must be positive integers.');
            return pair;
        });
    } else {
        const replicas = parse(replicaVariants, 'Replica variants');
        const prefill = isPd ? parse(prefillReplicaVariants, 'Prefill replica variants') : [0];
        pairs = prefill.flatMap(p => replicas.map(d => [p, d]));
    }
    if (pairs.length * prefillTps.length * tps.length > 16) throw new Error('A configuration sweep is limited to 16 topology combinations.');
    return pairs.flatMap(([prefillReplicaCount, replicaCount]) => prefillTps.flatMap(prefillTpCount => tps.map(tpCount => ({
        prefillReplicaCount, replicaCount, prefillTpCount, tpCount,
        gpuCount: prefillReplicaCount * prefillTpCount + replicaCount * tpCount,
    }))));
}

export const RUNTIME_CONTROL_FIELDS = {
    maxModelLen: 'max-model-len', maxNumSeqs: 'max-num-seqs',
    gpuMemoryUtilization: 'gpu-memory-utilization', blockSize: 'block-size',
    maxNumBatchedTokens: 'max-num-batched-tokens',
};

export function runtimeControlOverrides(values, modelServer) {
    if (modelServer !== 'vllm') return [];
    return Object.entries(RUNTIME_CONTROL_FIELDS)
        .filter(([field]) => values[field] != null && String(values[field]).trim() !== '')
        .map(([field, name]) => ({ target: 'both', kind: 'argument', name, value: String(values[field]) }));
}

export function configurationRuntimeValues(content) {
    const parameters = content.customParameters || content.custom_parameters || [];
    const decode = content.decode || content.serving || {};
    const defaults = {
        maxModelLen: decode.maxModelLen ?? decode.max_model_len ?? '',
        maxNumSeqs: decode.maxNumSeqs ?? decode.max_num_seqs ?? '',
    };
    return Object.fromEntries(Object.entries(RUNTIME_CONTROL_FIELDS).map(([field, name]) => {
        const matches = parameters.filter(item => item.kind === 'argument' && item.name === name);
        const shared = matches.find(item => item.target === 'both');
        const value = shared ? shared.value : matches.length ? '' : defaults[field] ?? '';
        return [field, String(value)];
    }));
}

/** Keep published configurations immutable while recording an edited manifest. */
export async function editedEvaluationConfiguration(artifact, manifest, { now = () => new Date().toISOString() } = {}) {
    const source = artifact.deployable_configuration;
    const content = {
        ...source.content,
        officialGuide: {
            ...(source.content?.officialGuide || {}),
            renderedManifest: manifest,
            manifestChecksum: await sha256(manifest),
        },
    };
    return {
        ...source,
        content,
        checksum: await configurationChecksum(content),
        provenance: {
            ...(source.provenance || {}),
            edited_from_artifact_id: artifact.artifact_id,
            edited_at: now(),
        },
    };
}
