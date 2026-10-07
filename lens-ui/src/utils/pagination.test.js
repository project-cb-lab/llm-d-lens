import test from 'node:test';
import assert from 'node:assert/strict';
import { paginate } from './pagination.js';
import { scaleBytes } from './formatBytes.js';

test('pagination clamps after deletion and represents empty collections', () => {
    assert.deepEqual(paginate([], 4, 10), { totalPages: 1, currentPage: 0, pageStart: 0, pageEnd: 0, pagedItems: [] });
    assert.deepEqual(paginate([1, 2, 3], 3, 2), { totalPages: 2, currentPage: 1, pageStart: 3, pageEnd: 3, pagedItems: [3] });
    assert.deepEqual(paginate([1, 2, 3], 0, 2).pagedItems, [1, 2]);
});
test('binary byte scaling preserves caller precision and unit cap', () => {
    assert.deepEqual(scaleBytes(1023), { value: 1023, unit: 'B', unitIndex: 0 });
    assert.deepEqual(scaleBytes(1536), { value: 1.5, unit: 'KiB', unitIndex: 1 });
    assert.equal(scaleBytes(1024 ** 4).unit, 'TiB');
    assert.deepEqual(scaleBytes(1024 ** 4, 3), { value: 1024, unit: 'GiB', unitIndex: 3 });
});
