import { applyRuntimeOverrides } from './configurationOverrides.ts';

type Container = { command?: string[]; args?: string[]; env?: Array<Record<string, unknown>> };
import { shellWords } from './shellInvocation.ts';
export { shellWords } from './shellInvocation.ts';

const quoteShell = (value: string) => `'${value.replaceAll("'", "'\"'\"'")}'`;
const tpNames = ['tensor-parallel-size', 'tensor_parallel_size', 'tp_size'];

export function runtimeArgument(container: Container, name: string): string | undefined {
    const parts = [...(container.command || []), ...(container.args || [])];
    const script = parts.find(part => /\bvllm\s+serve\b/.test(part));
    const values = script ? shellWords(script).map(word => word.value) : parts;
    const valuesFound: string[] = [];
    values.forEach((value, index) => {
        if (value.startsWith(`--${name}=`)) valuesFound.push(value.slice(name.length + 3));
        else if (value === `--${name}` && values[index + 1] != null) valuesFound.push(values[index + 1]);
    });
    if (valuesFound.length > 1) throw new Error(`Duplicate --${name} arguments are ambiguous.`);
    return valuesFound[0];
}

export function configureCpuCache(container: Container, gib: number, variant: string) {
    const connector = JSON.parse(runtimeArgument(container, 'kv-transfer-config') || '{}');
    if (variant === 'native/cpu/base') {
        if (connector.kv_connector !== 'OffloadingConnector') throw new Error('CPU capacity requires the native OffloadingConnector.');
        connector.kv_connector_extra_config = { ...connector.kv_connector_extra_config, cpu_bytes_to_use: Math.floor(gib * 1024 ** 3) };
        applyRuntimeOverrides(container, [{ target: 'both', kind: 'argument', name: 'kv-transfer-config', value: JSON.stringify(connector) }]);
    } else {
        if (!String(connector.kv_connector || '').startsWith('LMCacheConnector')) throw new Error('CPU capacity requires an LMCache connector.');
        if (container.env?.some(item => item.name === 'LMCACHE_CONFIG_FILE')) throw new Error('LMCache file configuration must expose capacity in its Guide; environment capacity cannot override it reliably.');
        applyRuntimeOverrides(container, [{ target: 'both', kind: 'environment', name: 'LMCACHE_MAX_LOCAL_CPU_SIZE', value: String(gib) }]);
    }
}

export function configureModelServer(container: Container, model: string, tp: number, identity: string, precise: boolean) {
    const command = container.command || [];
    const args = container.args || [];
    const shell = command.some(part => /(?:^|\/)(?:ba)?sh$/.test(part));
    let values: string[];
    let scriptArray: string[] | undefined;
    let scriptIndex = -1;
    let words: ReturnType<typeof shellWords> = [];
    if (shell) {
        const commandOption = command.findIndex(part => part === '-c' || part === '-lc');
        if (commandOption < 0) throw new Error('Unsupported model-server shell command.');
        scriptArray = command[commandOption + 1] != null ? command : args;
        scriptIndex = scriptArray === command ? commandOption + 1 : 0;
        words = shellWords(scriptArray[scriptIndex] || '');
        values = words.map(word => word.value);
        const start = values[0] === 'exec' ? 1 : 0;
        if (!/(?:^|\/)vllm$/.test(values[start] || '') || values[start + 1] !== 'serve') {
            throw new Error('Model-server configuration requires a recognizable vllm serve command.');
        }
    } else {
        values = [...command, ...args];
        if (command.length && (!/(?:^|\/)vllm$/.test(values[0]) || values[1] !== 'serve')) throw new Error('Cannot identify a supported model-server entrypoint in the selected Guide.');
    }

    const edits = new Map<number, string>();
    const set = (index: number, value: string) => { edits.set(index, value); values[index] = value; };
    let modelIndex = -1;
    for (let index = 0; index < values.length; index++) {
        if (/^--model(?:-path)?=/.test(values[index])) { set(index, values[index].replace(/=.*/, `=${model}`)); modelIndex = index; break; }
        if (/^--model(?:-path)?$/.test(values[index]) && values[index + 1] != null) { modelIndex = index + 1; set(modelIndex, model); break; }
    }
    if (modelIndex < 0) {
        const serve = values.indexOf('serve');
        modelIndex = serve >= 0 ? serve + 1 : command.length;
        if (!values[modelIndex] || values[modelIndex].startsWith('-')) throw new Error('Cannot identify the model argument in the selected Guide.');
        set(modelIndex, model);
    }
    // A served name is part of model identity as well, including shared-path mode.
    for (let index = 0; index < values.length; index++) {
        if (values[index].startsWith('--served-model-name=')) set(index, `--served-model-name=${identity}`);
        else if (values[index] === '--served-model-name' && values[index + 1] != null) set(index + 1, identity);
        if (precise && (values[index] === '--kv-events-config' || values[index].startsWith('--kv-events-config='))) {
            const inline = values[index].includes('=');
            const target = inline ? index : index + 1;
            let config;
            try { config = JSON.parse(inline ? values[index].slice('--kv-events-config='.length) : values[target]); }
            catch { throw new Error('Precise routing requires a valid JSON kv-events-config.'); }
            if (typeof config.topic !== 'string' || !/^kv@[^@]+@.+$/.test(config.topic)) throw new Error('Precise routing requires a kv@host:port@model event topic.');
            config.topic = config.topic.replace(/@[^@]+$/, () => `@${identity}`);
            set(target, `${inline ? '--kv-events-config=' : ''}${JSON.stringify(config)}`);
        }
        for (const name of tpNames.slice(1)) {
            if (values[index] === `--${name}` || values[index].startsWith(`--${name}=`)) set(index, values[index].replace(`--${name}`, '--tensor-parallel-size'));
        }
    }
    if (scriptArray) {
        let script = scriptArray[scriptIndex];
        for (const [index, value] of [...edits].sort(([left], [right]) => right - left)) {
            // Keep flags recognizable by the shared override writer.
            const replacement = value.startsWith('--') && !/[\s'"$]/.test(value) ? value : quoteShell(value);
            script = script.slice(0, words[index].start) + replacement + script.slice(words[index].end);
        }
        scriptArray[scriptIndex] = script;
    } else {
        for (const [index, value] of edits) {
            if (index < command.length) command[index] = value;
            else args[index - command.length] = value;
        }
    }
    if (tp > 0) applyRuntimeOverrides(container, [{ target: 'both', kind: 'argument', name: 'tensor-parallel-size', value: String(tp) }]);
    if (model !== identity) applyRuntimeOverrides(container, [{ target: 'both', kind: 'argument', name: 'served-model-name', value: identity }]);
}
