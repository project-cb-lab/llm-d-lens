import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { storagePath } from './storagePaths.ts';

test('Lens path precedence matches Python contract', () => {
    const env = { XDG_DATA_HOME: '/tmp/xdg', LENS_CACHE_DIR: '/tmp/cache' };
    assert.equal(storagePath('data', ['artifacts'], env), '/tmp/xdg/lens/artifacts');
    assert.equal(storagePath('scratch', ['render'], env), '/tmp/cache/tmp/render');
    assert.equal(storagePath('data', ['ignored'], { ...env, OLD_ROOT: '/tmp/old' }), '/tmp/xdg/lens/ignored');
    assert.equal(storagePath('data', [], {}), path.join(os.homedir(), '.local/share/lens'));
    assert.throws(() => storagePath('data', ['../escape']), /relative/);
});
