import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import EvaluationConfigurationEditor from './EvaluationConfigurationEditor.jsx';

test('configuration editor renders after environment setup with auto-cache selected', (t) => {
    const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
    Object.defineProperty(globalThis, 'sessionStorage', {
        configurable: true,
        value: { getItem: () => null, removeItem() {} },
    });
    t.after(() => {
        if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage);
        else delete globalThis.sessionStorage;
    });
    const html = renderToStaticMarkup(createElement(EvaluationConfigurationEditor, {
        sharedContext: {
            model: 'Qwen/Qwen3-0.6B',
            modelSource: 'auto-cache',
            cluster: { id: 'test-cluster', name: 'Test cluster', sessionId: 'test-session' },
            modelServer: 'vllm',
            image: 'runtime:v1',
            storageVolumeId: 'test-volume',
            cacheValidation: { state: 'ready' },
        },
    }));

    assert.match(html, /Qwen\/Qwen3-0.6B/);
    assert.match(html, /Test cluster/);
    assert.match(html, /Generate YAML/);
    assert.doesNotMatch(html, /Use AIC for PD|AIC P\/D recommendation|Apply P:D ratios/);
});
