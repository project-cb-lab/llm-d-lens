import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const session = '00000000-0000-0000-0000-000000000001';
test('cluster session paths use Lens roots even when old path variables are set', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lens-session-'));
    try {
        for (const [env, relative] of [
            [{ PRISM_CLUSTER_SESSION_DIR: '~/sessions' }, '.local/share/lens/credentials/clusters/cluster_sessions'],
            [{ LLM_D_BENCH_DATA_DIR: '~/legacy-clusters' }, '.local/share/lens/credentials/clusters/cluster_sessions'],
            [{ LENS_DATA_DIR: '~/data' }, 'data/credentials/clusters/cluster_sessions'],
        ] as Array<[NodeJS.ProcessEnv, string]>) {
            const expected = path.join(root, relative, `${session}.yaml`);
            fs.mkdirSync(path.dirname(expected), { recursive: true });
            fs.writeFileSync(expected, 'test');
            const code = `import { clusterSessionKubeconfig } from './server/clusterSession.ts'; process.stdout.write(clusterSessionKubeconfig('${session}') || 'missing');`;
            const output = execFileSync(process.execPath, ['--import', 'tsx', '--input-type=module', '-e', code], {
                env: { PATH: process.env.PATH, HOME: root, ...env }, encoding: 'utf8',
            });
            assert.equal(output, expected);
        }
    } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
