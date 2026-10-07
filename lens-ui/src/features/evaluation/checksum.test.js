import assert from 'node:assert/strict';
import test from 'node:test';
import { configurationChecksum } from './checksum.js';

test('checksum ignores object key order but preserves arrays and values', async () => {
    assert.equal(await configurationChecksum({b:2,a:{z:0,y:1}}),await configurationChecksum({a:{y:1,z:0},b:2}));
    assert.notEqual(await configurationChecksum([1,2]), await configurationChecksum([2,1]));
    assert.equal(await configurationChecksum({}), 'sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a');
});
