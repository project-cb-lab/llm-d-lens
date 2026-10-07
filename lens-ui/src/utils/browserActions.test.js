import test from 'node:test';
import assert from 'node:assert/strict';
import { copyText } from './clipboard.js';
import { downloadBlob } from './download.js';

function globals(t, values) {
    for (const [key, value] of Object.entries(values)) {
        const original = Object.getOwnPropertyDescriptor(globalThis, key);
        Object.defineProperty(globalThis, key, {value, configurable: true});
        t.after(() => original ? Object.defineProperty(globalThis, key, original) : delete globalThis[key]);
    }
}

test('copy uses clipboard API; denial can use fallback and restores focus', async (t) => {
    let copied;
    globals(t, {navigator: {clipboard: {writeText: async (text) => {copied = text;}}}});
    await copyText('endpoint');
    assert.equal(copied, 'endpoint');
    let removed = false;
    let restored = false;
    const area = {style: {}, focus() {}, select() {}, remove() {removed = true;}};
    globals(t, {document: {activeElement: {focus() {restored = true;}}, createElement: () => area, body: {appendChild() {}}, execCommand: () => true}});
    navigator.clipboard.writeText = async () => {throw new Error('denied');};
    await copyText('fallback');
    assert.equal(area.value, 'fallback');
    assert.equal(removed, true);
    assert.equal(restored, true);
    document.execCommand = () => false;
    await assert.rejects(copyText('failure'), /Copy failed/);
});
test('copy works without secure-context API and removes textarea on exceptions', async (t) => {
    let removed = false;
    globals(t, {navigator: {}, document: {createElement: () => ({style: {}, focus() {}, select() {}, remove() {removed = true;}}), body: {appendChild() {}}, execCommand() {throw new Error('unavailable');}}});
    await assert.rejects(copyText('endpoint'), /unavailable/);
    assert.equal(removed, true);
});
test('download delays URL revocation until browser can consume it and removes anchor', (t) => {
    t.mock.timers.enable({apis: ['setTimeout']});
    const revoked = [];
    let clicked = false;
    let removed = false;
    const anchor = {click() {clicked = true;}, remove() {removed = true;}};
    globals(t, {document: {createElement: () => anchor, body: {appendChild() {}}}});
    t.mock.method(URL, 'createObjectURL', () => 'blob:test');
    t.mock.method(URL, 'revokeObjectURL', url => revoked.push(url));
    downloadBlob(new Blob(['report']), 'report.md');
    assert.equal(anchor.download, 'report.md');
    assert.equal(clicked && removed, true);
    assert.deepEqual(revoked, []);
    t.mock.timers.tick(1000);
    assert.deepEqual(revoked, ['blob:test']);
});
