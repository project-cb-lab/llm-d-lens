import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { guideFor, prepareRepositoryCommand } from './remoteDeploy.ts';

test('deployment guide rejects shell syntax and accepts guide identifiers', () => {
    assert.equal(guideFor({ guide: 'optimized-baseline' }), 'optimized-baseline');
    assert.throws(() => guideFor({ guide: 'optimized-baseline; touch /tmp/injected' }), /Invalid deployment guide/);
});

test('remote explicit home paths expand HOME without evaluating their contents', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lens-remote-'));
    try {
        const suffix = "repo ' $(touch injected)";
        fs.mkdirSync(path.join(root, suffix, '.git'), { recursive: true });
        const { command } = prepareRepositoryCommand({ deploymentTarget: { repository: `~/${suffix}` } });
        const output = execFileSync('bash', ['-c', `git() { :; }; ${command}; printf '%s' "$LLM_D_ROOT"`], {
            env: { PATH: '/usr/bin:/bin', HOME: root }, cwd: root, encoding: 'utf8',
        });
        assert.equal(output, path.join(root, suffix));
        assert.equal(fs.existsSync(path.join(root, 'injected')), false);
    } finally { fs.rmSync(root, { recursive: true, force: true }); }
});


test('remote clone uses remote Lens cache and includes repository owner', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lens-remote-path-'));
    try {
        for (const [extra, expected] of [
            [{ LENS_CACHE_DIR: `${root}/custom cache` }, `${root}/custom cache`],
            [{ XDG_CACHE_HOME: `${root}/xdg` }, `${root}/xdg/lens`],
            [{}, `${root}/.cache/lens`],
        ] as Array<[NodeJS.ProcessEnv, string]>) {
            const { command } = prepareRepositoryCommand({ deploymentTarget: { repository: 'https://github.com/llm-d/llm-d.git' } });
            const output = execFileSync('bash', ['-c', `git() { :; }; ${command}; printf '%s' "$LLM_D_ROOT"`], {
                env: { PATH: '/usr/bin:/bin', HOME: root, ...extra }, encoding: 'utf8',
            });
            assert.equal(output, `${expected}/deploy-repos/llm-d/llm-d`);
        }
    } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
