import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { buildCliDeployCommand } from './deploy.ts';

test('deploy command treats executable and repository environment values as literal arguments', () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'lens-deploy-command-'));
    const marker = path.join(directory, 'injected');
    const payload = `; touch ${marker}; #`;
    try {
        const command = buildCliDeployCommand(payload, 'prism-deploy-poc', payload, payload);
        spawnSync('bash', ['-c', command], { stdio: 'ignore' });
        assert.equal(fs.existsSync(marker), false);
    } finally {
        fs.rmSync(directory, { recursive: true, force: true });
    }
});