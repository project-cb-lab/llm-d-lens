import assert from 'node:assert/strict';
import test from 'node:test';
import { createGuideManifestCache } from './guideManifestCache.ts';

test('concurrent and repeated variants share one immutable source load', async () => {
    const cached = createGuideManifestCache<string>();
    let calls = 0;
    const load = async () => { calls++; return 'source YAML'; };
    await Promise.all([cached('repo/commit-a/guide', load), cached('repo/commit-a/guide', load)]);
    await cached('repo/commit-a/guide', load);
    assert.equal(calls, 1);
    await cached('repo/commit-b/guide', load);
    assert.equal(calls, 2);
});

test('failed downloads retry, local sources bypass the cache, and old entries are bounded', async () => {
    const cached = createGuideManifestCache<string>(2);
    await assert.rejects(cached('a', async () => { throw new Error('download failed'); }));
    assert.equal(await cached('a', async () => 'retry'), 'retry');
    let calls = 0;
    const local = async () => String(++calls);
    assert.equal(await cached(null, local), '1');
    assert.equal(await cached(null, local), '2');
    await cached('b', local);
    await cached('c', local);
    assert.equal(await cached('a', async () => 'reloaded'), 'reloaded');
});


test('immutable sources survive process-cache recreation and corrupt disk entries are repaired', async () => {
    const { mkdtemp, rm, readdir, writeFile } = await import('node:fs/promises');
    const { tmpdir } = await import('node:os');
    const { join } = await import('node:path');
    const directory = await mkdtemp(join(tmpdir(), 'prism-manifest-cache-test-'));
    const options = { directory, validate: (value: unknown): value is string => typeof value === 'string' };
    try {
        const original = createGuideManifestCache<string>(2, options);
        await original('commit/guide', async () => 'source');
        const restarted = createGuideManifestCache<string>(2, options);
        assert.equal(await restarted('commit/guide', async () => { throw new Error('Must use persisted source'); }), 'source');
        const file = (await readdir(directory))[0];
        await writeFile(join(directory, file), '{"wrong":"shape"}');
        const repaired = createGuideManifestCache<string>(2, options);
        assert.equal(await repaired('commit/guide', async () => 'reloaded'), 'reloaded');
    } finally { await rm(directory, { recursive: true, force: true }); }
});
