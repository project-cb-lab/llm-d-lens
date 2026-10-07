import assert from 'node:assert/strict';
import test from 'node:test';
import { validateImportManifest } from './importManifest.js';

test('rejects malformed YAML with a line and column before import', () => {
    assert.throws(() => validateImportManifest('kind: [\n'), /line \d+, column \d+/);
});
test('rejects empty files and non-resource documents', () => {
    for (const text of ['', '# comment', 'hello', '- item', 'kind: Deployment', 'apiVersion: v1\nkind: Service\nmetadata: {}']) {
        assert.throws(() => validateImportManifest(text));
    }
});
test('accepts multiple Kubernetes resources and checks every document', () => {
    const resource = 'apiVersion: v1\nkind: Service\nmetadata:\n  name: serving\n';
    assert.doesNotThrow(() => validateImportManifest(`${resource}---\n${resource}`));
    assert.throws(() => validateImportManifest(`${resource}---\ninvalid`), /Document 2/);
});
