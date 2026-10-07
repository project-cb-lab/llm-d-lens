import yaml from 'js-yaml';
import { expandConfigurationTopologies, RUNTIME_CONTROL_FIELDS, runtimeControlOverrides } from './configuration.js';
import { normalizeGuideSettings } from './guideSettings.js';
import { adminManagedEnvironmentMessage, isAdminManagedEnvironmentVariable } from './managedEnvironment.js';

/** Field identifiers are shared with the editor, so validation never depends on label text. */
export function validateConfigurationInputs(input) {
    const { guide, guideVariant, guideVariants = [], guideSettings = {}, customRows = [], runtime = {}, modelServer } = input;
    const errors = [];
    const add = (field, message) => {
        if (!errors.some(error => error.field === field && error.message === message)) errors.push({ field, message });
    };
    const isPd = guide === 'pd-disaggregation';
    for (const field of ['cacheCpuGiB', 'rdmaNicCount', 'routerValues']) {
        try { normalizeGuideSettings(guide, guideVariant, { [field]: guideSettings[field] }); }
        catch (error) { add(field, error.message); }
    }
    if (guideSettings.routerValues?.trim()) {
        try {
            const parsed = yaml.load(guideSettings.routerValues);
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Enter a YAML mapping, such as router: { epp: { replicas: 1 } }.');
        } catch (error) {
            add('routerValues', error.mark ? `Line ${error.mark.line + 1}, column ${error.mark.column + 1}: ${error.reason}` : error.message);
        }
    }
    if (isPd && guideVariants.length && !guideVariants.includes(guideVariant)) add('guideVariant', 'Select a supported P/D variant.');
    if (guide === 'tiered-prefix-cache' && !guideVariants.includes(guideVariant)) add('guideVariant', 'Select a supported cache variant.');
    const topologyFields = ['tensorParallelVariants', ...(isPd ? ['prefillTensorParallelVariants'] : []), ...(!isPd || !input.pdTopologyVariants?.trim() ? ['replicaVariants', ...(isPd ? ['prefillReplicaVariants'] : [])] : [])];
    for (const field of topologyFields) {
        if (String(input[field]).split(',').some(value => !value.trim() || !Number.isSafeInteger(Number(value)) || Number(value) < 1)) add(field, 'Enter positive integers separated by commas.');
    }
    if (!errors.some(error => topologyFields.includes(error.field))) {
        try {
            // Validate topology syntax here; generation checks the explicitly selected resource budget.
            expandConfigurationTopologies(input);
        } catch (error) { add(isPd && input.pdTopologyVariants?.trim() ? 'pdTopologyVariants' : 'replicaVariants', error.message); }
    }
    const protectedNames = ['model', 'model-path', 'served-model-name', 'tensor-parallel-size', 'tensor_parallel_size', 'tp_size'];
    customRows.forEach((item, index) => {
        const field = part => `custom.${index}.${part}`;
        if (!['prefill', 'decode', 'both'].includes(item.target)) add(field('target'), 'Select a target.');
        else if (!isPd && item.target === 'prefill') add(field('target'), 'Prefill is only available with the P/D Guide.');
        if (!['argument', 'environment'].includes(item.kind)) add(field('kind'), 'Select Argument or Environment.');
        if (!item.name?.trim()) add(field('name'), 'Enter a parameter name.');
        else if (!(item.kind === 'environment' ? /^[A-Za-z_][A-Za-z0-9_]*$/ : /^[A-Za-z_][A-Za-z0-9_-]*$/).test(item.name)) add(field('name'), item.kind === 'environment' ? 'Use letters, digits and underscores; start with a letter or underscore.' : 'Omit --; use letters, digits, underscores or hyphens.');
        else if (item.kind === 'argument' && protectedNames.includes(item.name)) add(field('name'), 'Set model and TP using the dedicated controls.');
        else if (item.kind === 'environment' && isAdminManagedEnvironmentVariable(item.name)) add(field('name'), adminManagedEnvironmentMessage(item.name));
        if (item.value == null) add(field('value'), 'Enter a value (an empty string is allowed).');
    });
    const shared = runtimeControlOverrides(runtime, modelServer).map(item => ({ ...item, field: Object.keys(RUNTIME_CONTROL_FIELDS).find(key => RUNTIME_CONTROL_FIELDS[key] === item.name), label: `the shared ${item.name} control` }));
    const overrides = [...shared, ...customRows.map((item, index) => ({ ...item, field: `custom.${index}.name`, label: `override ${index + 1}` }))];
    overrides.forEach((item, index) => {
        if (!item.name?.trim()) return;
        overrides.slice(0, index).forEach(previous => {
            if (previous.kind === item.kind && previous.name === item.name && (!isPd || previous.target === item.target || previous.target === 'both' || item.target === 'both')) {
                add(item.field, `Conflicts with ${previous.label}. Keep one value per role.`);
                add(previous.field, `Conflicts with ${item.label}. Keep one value per role.`);
            }
        });
    });
    if (modelServer === 'vllm') {
        for (const field of ['maxModelLen', 'maxNumSeqs', 'blockSize', 'maxNumBatchedTokens']) {
            const value = runtime[field];
            if (value != null && value !== '' && (!Number.isSafeInteger(Number(value)) || Number(value) < 1)) add(field, 'Enter a positive integer, or leave blank to inherit the Guide.');
        }
        const value = runtime.gpuMemoryUtilization;
        if (value != null && value !== '' && !(Number(value) > 0 && Number(value) <= 1)) add('gpuMemoryUtilization', 'Enter a fraction greater than 0 and at most 1 (for example, 0.9).');
    }
    return errors;
}

// The planning API currently returns strings. Only attach errors with a known
// parameter identity; infrastructure failures remain global.
export function configurationServerErrors(messages, { customRows = [], sweepEnabled = false, pdTopologyVariants = '', embedded = false } = {}) {
    return messages.flatMap(message => {
        const text = String(message);
        const custom = customRows.findIndex(row => row.name && text.includes(row.name));
        let field;
        if (/router|EPP|token-producer|precise-prefix-cache plugin/i.test(text)) field = 'routerValues';
        else if (/CPU cache|cacheCpuGiB/i.test(text)) field = 'cacheCpuGiB';
        else if (/NIC|rdmaNicCount/i.test(text)) field = 'rdmaNicCount';
        else if (custom >= 0) field = `custom.${custom}.name`;
        else if (Object.values(RUNTIME_CONTROL_FIELDS).some(name => text.includes(name))) field = Object.keys(RUNTIME_CONTROL_FIELDS).find(key => text.includes(RUNTIME_CONTROL_FIELDS[key]));
        else if (/block.size/i.test(text)) field = 'blockSize';
        else if (/prefill.*TP|prefill.*tensor.parallel/i.test(text)) field = sweepEnabled ? 'prefillTensorParallelVariants' : 'prefillTensorParallelSize';
        else if (/\bTP\b|tensor.parallel/i.test(text)) field = sweepEnabled ? 'tensorParallelVariants' : 'tensorParallelSize';
        else if (/topology needs|replicas=.*exceed/i.test(text)) field = sweepEnabled ? (pdTopologyVariants.trim() ? 'pdTopologyVariants' : /prefill/i.test(text) ? 'prefillReplicaVariants' : 'replicaVariants') : /prefill/i.test(text) ? 'prefillReplicas' : 'replicas';
        else if (/Shared model path/i.test(text)) field = embedded ? 'setup' : 'modelPath';
        return field ? [{ field, message: text }] : [];
    });
}
