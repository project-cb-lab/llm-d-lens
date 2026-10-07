import { shellWords } from './shellInvocation.ts';
import { adminManagedEnvironmentMessage, isAdminManagedEnvironmentVariable } from '../src/features/evaluation/managedEnvironment.js';

type Override = { target: string; kind: string; name: string; value: string };

const dedicatedArguments = new Set(['model', 'model-path', 'served-model-name', 'tensor-parallel-size', 'tensor_parallel_size', 'tp_size']);

export function runtimeOverrides(request: Record<string, unknown>): Override[] {
    const custom = request.customParameters ?? [];
    if (!Array.isArray(custom)) throw new Error('Custom runtime overrides must be an array.');
    const result: Override[] = [];
    for (const [field, name] of [['maxModelLen', 'max-model-len'], ['maxNumSeqs', 'max-num-seqs'], ['gpuMemoryUtilization', 'gpu-memory-utilization'], ['blockSize', 'block-size'], ['maxNumBatchedTokens', 'max-num-batched-tokens']]) {
        if (request[field] == null || String(request[field]).trim() === '') continue;
        if (request.modelServer && request.modelServer !== 'vllm') throw new Error(`${field} is a vLLM control; inherit the selected runtime or use its native arguments.`);
        const value = Number(request[field]);
        if (field === 'gpuMemoryUtilization' ? !(value > 0 && value <= 1) : (!Number.isSafeInteger(value) || value < 1)) throw new Error(`${field} must be a positive integer.`);
        result.push({ target: 'both', kind: 'argument', name, value: String(value) });
    }
    for (const item of custom) {
        if (!item || !['prefill', 'decode', 'both'].includes(item.target) || !['argument', 'environment'].includes(item.kind)
            || typeof item.name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_-]*$/.test(item.name)
            || item.value == null || !['string', 'number', 'boolean'].includes(typeof item.value)) {
            throw new Error('Each runtime override requires a valid target, kind, name and scalar value.');
        }
        if (item.kind === 'argument' && dedicatedArguments.has(item.name)) throw new Error(`Use the dedicated configuration control for ${item.name}.`);
        if (item.kind === 'environment' && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(item.name)) throw new Error(`Invalid environment variable ${item.name}.`);
        if (item.kind === 'environment' && isAdminManagedEnvironmentVariable(item.name)) throw new Error(adminManagedEnvironmentMessage(item.name));
        if (result.some(previous => previous.kind === item.kind && previous.name === item.name
            && (previous.target === item.target || previous.target === 'both' || item.target === 'both'))) throw new Error(`Overlapping runtime override: ${item.name}.`);
        result.push({ ...item, value: String(item.value) });
    }
    return result;
}

function quoted(value: string) {
    return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

export function applyRuntimeOverrides(container: { command?: string[]; args?: string[]; env?: Array<Record<string, unknown>> }, overrides: Override[]) {
    for (const item of overrides) {
        if (item.kind === 'environment') {
            container.env = [...(container.env || []).filter((entry) => entry.name !== item.name), { name: item.name, value: item.value }];
            continue;
        }
        const command = container.command || [];
        const args = container.args || [];
        const scriptArray = [command, args].find(parts => parts.some((part: unknown) => typeof part === 'string' && /\bvllm\s+serve\b/.test(part)));
        if (scriptArray) {
            const index = scriptArray.findIndex((part: unknown) => typeof part === 'string' && /\bvllm\s+serve\b/.test(part));
            let script = scriptArray[index];
            if (item.name === 'max-num-seqs') script = script.replace(/--max-num-seq(?==|\s|$)/g, '--max-num-seqs');
            const words = shellWords(script);
            const start = words[0]?.value === 'exec' ? 1 : 0;
            if (!/(?:^|\/)vllm$/.test(words[start]?.value || '') || words[start + 1]?.value !== 'serve') throw new Error('Runtime overrides require a single vllm serve invocation.');
            const removals: Array<[number, number]> = [];
            words.forEach((word, wordIndex) => {
                if (word.value.startsWith(`--${item.name}=`)) removals.push([word.start, word.end]);
                else if (word.value === `--${item.name}`) {
                    const next = words[wordIndex + 1];
                    const hasValue = next && !next.value.startsWith('--');
                    if (!hasValue && item.value !== '') throw new Error(`Existing --${item.name} is valueless; leave its value empty or use the Guide's explicit disabling flag.`);
                    removals.push([word.start, hasValue ? next.end : word.end]);
                }
            });
            const value = /^[A-Za-z0-9_./:-]+$/.test(item.value) ? item.value : quoted(item.value);
            const flag = `--${item.name}${item.value === '' ? '' : `=${value}`}`;
            if (removals.length > 1) throw new Error(`Duplicate --${item.name} arguments are ambiguous.`);
            if (removals.length) {
                const [from, to] = removals[0];
                scriptArray[index] = script.slice(0, from) + flag + script.slice(to);
            } else {
                const tail = script.trimEnd();
                scriptArray[index] = `${tail}${tail.endsWith('\\') ? '\n' : ' '} ${flag}`;
            }
        } else {
            if (command.some((part: unknown) => typeof part === 'string' && /(?:^|\/)(?:ba)?sh$/.test(part))) {
                throw new Error('Runtime argument overrides require a recognizable vllm serve command.');
            }
            const flag = `--${item.name}`;
            for (const parts of [command, args]) {
                for (let index = parts.length - 1; index >= 0; index--) {
                    if (parts[index] === flag) {
                        const hasValue = parts[index + 1] != null && !String(parts[index + 1]).startsWith('--');
                        if (!hasValue && item.value !== '') throw new Error(`Existing ${flag} is valueless; leave its value empty.`);
                        parts.splice(index, hasValue ? 2 : 1);
                    }
                    else if (String(parts[index]).startsWith(`${flag}=`)) parts.splice(index, 1);
                }
            }
            container.args = [...args, item.value === '' ? flag : `${flag}=${item.value}`];
        }
    }
}
