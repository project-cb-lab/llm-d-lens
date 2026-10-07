import assert from 'node:assert/strict';
import test from 'node:test';

import { loadProfile, parseEnvSh, runChecks, validateStatic } from './check-llm-d-stack.mjs';

test('the shipped profile has every required version', () => {
    assert.deepEqual(validateStatic(loadProfile()), []);
});

test('static validation reports missing versions', () => {
    const problems = validateStatic({ llm_d: 'v1', gateway_providers: {} });
    assert.ok(problems.some((problem) => problem.includes('llm_d_router')));
    assert.ok(problems.some((problem) => problem.includes('gateway_providers.istio')));
});

test('env.sh parsing reads the pinned defaults', () => {
    const parsed = parseEnvSh([
        'export ROUTER_CHART_VERSION=${ROUTER_CHART_VERSION:-v0.10.0}',
        'export GATEWAY_API_VERSION=${GATEWAY_API_VERSION:-v1.5.1}',
        'export GAIE_VERSION=${GAIE_VERSION:-v1.5.0}',
    ].join('\n'));
    assert.deepEqual(parsed, {
        ROUTER_CHART_VERSION: 'v0.10.0',
        GATEWAY_API_VERSION: 'v1.5.1',
        GAIE_VERSION: 'v1.5.0',
    });
});

test('offline checks pass for the shipped profile and skip the network', async () => {
    const log = [];
    const { failures } = await runChecks(loadProfile(), { offline: true, log: (line) => log.push(line) });
    assert.deepEqual(failures, []);
    assert.ok(log.some((line) => line.includes('skipping network checks')));
});
