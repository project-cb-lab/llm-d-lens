import assert from 'node:assert/strict';
import test from 'node:test';
import { normalizeGuideSettings } from './guideSettings.js';

test('guide-specific options reject incompatible variants and malformed values', () => {
    assert.throws(() => normalizeGuideSettings('optimized-baseline', '', { cacheCpuGiB: 20 }));
    assert.throws(() => normalizeGuideSettings('pd-disaggregation', 'vllm', { rdmaNicCount: 2 }));
    assert.throws(() => normalizeGuideSettings('pd-disaggregation', 'vllm-rdma', { rdmaNicCount: 1.5 }));
    assert.deepEqual(normalizeGuideSettings('tiered-prefix-cache', 'native/cpu/base', { cacheCpuGiB: '20', routerValues: 'router: {}' }), { cacheCpuGiB: 20, routerValues: 'router: {}' });
    assert.deepEqual(normalizeGuideSettings('optimized-baseline', '', { cacheCpuGiB: '', rdmaNicCount: '' }), {});
});
