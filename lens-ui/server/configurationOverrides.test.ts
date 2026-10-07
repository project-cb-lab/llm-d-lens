import assert from 'node:assert/strict';
import test from 'node:test';
import { execFileSync } from 'node:child_process';
import { applyRuntimeOverrides, runtimeOverrides } from './configurationOverrides.ts';

test('a bare flag never consumes the following option in shell or argv', () => {
    for (const container of [
        { command: ['bash', '-c'], args: ['exec vllm serve Model --enable-prefix-caching --port 8000'] },
        { args: ['Model', '--enable-prefix-caching', '--port', '8000'] },
    ]) {
        assert.throws(() => applyRuntimeOverrides(container, runtimeOverrides({ customParameters: [{ target: 'both', kind: 'argument', name: 'enable-prefix-caching', value: 'false' }] })), /valueless/);
        assert.ok(container.args.join(' ').includes('--port 8000'));
    }
});
test('empty argument value represents a flag', () => {
    const container = { args: ['Model', '--port', '8000'] };
    applyRuntimeOverrides(container, runtimeOverrides({ customParameters: [{ target: 'both', kind: 'argument', name: 'enable-prefix-caching', value: '' }] }));
    assert.deepEqual(container.args, ['Model', '--port', '8000', '--enable-prefix-caching']);
});
test('dedicated controls and overlapping overrides cannot silently shadow configuration facts', () => {
    assert.throws(() => runtimeOverrides({ customParameters: [{ target: 'both', kind: 'argument', name: 'tensor-parallel-size', value: '8' }] }), /dedicated/);
    assert.throws(() => runtimeOverrides({ customParameters: [
        { target: 'both', kind: 'environment', name: 'FLAG', value: '1' },
        { target: 'decode', kind: 'environment', name: 'FLAG', value: '2' },
    ] }), /Overlapping/);
});

test('administrator-managed Hugging Face environment variables are rejected', () => {
    for (const name of ['HF_HOME', 'HF_TOKEN', 'TRANSFORMERS_OFFLINE', 'HUGGINGFACE_HUB_CACHE']) {
        assert.throws(() => runtimeOverrides({ customParameters: [{ target: 'both', kind: 'environment', name, value: 'x' }] }), /platform administrator/, name);
    }
    assert.deepEqual(runtimeOverrides({ customParameters: [{ target: 'both', kind: 'environment', name: 'VLLM_LOGGING_LEVEL', value: 'DEBUG' }] }), [{ target: 'both', kind: 'environment', name: 'VLLM_LOGGING_LEVEL', value: 'DEBUG' }]);
});

test('shell overrides execute as literal arguments without expansion', () => {
    const container = { command: ['bash', '-c'], args: ['vllm serve Model --port 8000'] };
    const literal = "a 'quoted' value $(printf expanded)";
    applyRuntimeOverrides(container, runtimeOverrides({ blockSize: 64, customParameters: [
        { target: 'both', kind: 'argument', name: 'download-dir', value: literal },
    ] }));
    const output = execFileSync('bash', ['-c', `vllm() { printf '%s\\0' "$@"; }\n${container.args[0]}`], { encoding: 'utf8' });
    assert.deepEqual(output.split('\0').slice(0, -1), ['serve', 'Model', '--port', '8000', '--block-size=64', `--download-dir=${literal}`]);
});

test('non-vLLM runtime rejects vLLM controls instead of emitting incompatible YAML', () => {
    assert.throws(() => runtimeOverrides({ modelServer: 'sglang', blockSize: 64 }), /vLLM/);
    assert.deepEqual(runtimeOverrides({ modelServer: 'sglang' }), []);
});
test('empty controls inherit the source and role-specific tuning can be supplied explicitly', () => {
    assert.deepEqual(runtimeOverrides({ blockSize: '', maxNumSeqs: '' }), []);
    assert.deepEqual(runtimeOverrides({ customParameters: [{ target: 'prefill', kind: 'argument', name: 'max-num-seqs', value: '128' }] }), [{ target: 'prefill', kind: 'argument', name: 'max-num-seqs', value: '128' }]);
    assert.throws(() => runtimeOverrides({ maxNumSeqs: 64, customParameters: [{ target: 'prefill', kind: 'argument', name: 'max-num-seqs', value: '128' }] }), /Overlapping/);
});
