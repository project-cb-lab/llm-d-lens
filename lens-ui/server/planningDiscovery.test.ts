import assert from 'node:assert/strict';
import test from 'node:test';
import { inspectPlanningCluster, type DiscoveryResult } from './planningDiscovery.ts';

test('all independent cluster reads start before any finishes', async () => {
    // eslint-disable-next-line no-unused-vars -- TypeScript function parameter names.
    const waiting: Array<(value: DiscoveryResult | null) => void> = [];
    const result = inspectPlanningCluster('cluster-a', async (cluster) => {
        assert.equal(cluster, 'cluster-a');
        return new Promise((resolve) => waiting.push(resolve));
    });
    // version, nodes, deviceclasses, resourceslices, runtimeclasses.
    assert.equal(waiting.length, 5);
    waiting[0]({ serverVersion: { gitVersion: 'v1' } });
    waiting[1]({ items: [] });
    waiting[2](null);
    waiting[3](null);
    waiting[4](null);
    assert.ok(await result);
});

test('a failed required cluster read must not be treated as a successful preflight', async () => {
    assert.equal(await inspectPlanningCluster(null, async (_, args) => args[1] === 'nodes' ? null : {}), null);
});

test('selected sessions use the Python read boundary and never send kubeconfig paths', async () => {
    const fs = await import('node:fs');
    const os = await import('node:os');
    const path = await import('node:path');
    const { execFile } = await import('node:child_process');
    const { promisify } = await import('node:util');
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'planning-session-'));
    const id = '00000000-0000-0000-0000-000000000001';
    const sessions = path.join(root, 'credentials/clusters/cluster_sessions');
    fs.mkdirSync(sessions, { recursive: true });
    fs.writeFileSync(path.join(sessions, `${id}.yaml`), 'private');
    try {
        const code = `
            import assert from 'node:assert/strict';
            import { queryCluster } from './server/planningDiscovery.ts';
            const config = process.env.LENS_DATA_DIR + '/credentials/clusters/cluster_sessions/${id}.yaml';
            let calls = 0;
            globalThis.fetch = async (url, init) => {
                assert.equal(String(url), 'http://backend.invalid/api/cluster/sessions/${id}/planning-discovery/nodes');
                assert.equal(init.method, 'GET');
                assert.equal(init.body, undefined);
                calls++;
                return new Response(JSON.stringify({ result: { items: [{ metadata: { name: 'scoped' } }] } }));
            };
            assert.deepEqual(await queryCluster(config, ['get', 'nodes', '-o', 'json']), { items: [{ metadata: { name: 'scoped' } }] });
            assert.equal(calls, 1);
            globalThis.fetch = async () => new Response('failed', { status: 503 });
            assert.equal(await queryCluster(config, ['get', 'nodes', '-o', 'json']), null);
            globalThis.fetch = async () => new Response('not JSON');
            assert.equal(await queryCluster(config, ['get', 'nodes', '-o', 'json']), null);
            globalThis.fetch = async () => { throw new Error('aborted'); };
            assert.equal(await queryCluster(config, ['get', 'nodes', '-o', 'json']), null);
        `;
        await promisify(execFile)(process.execPath, ['--import', 'tsx', '--input-type=module', '-e', code], {
            env: { ...process.env, LENS_DATA_DIR: root, SIMULATION_API_URL: 'http://backend.invalid', PATH: '' },
        });
    } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
