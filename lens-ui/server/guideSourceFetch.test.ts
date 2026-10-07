import assert from 'node:assert/strict';
import test from 'node:test';
import { readGuideSource } from './guideSourceFetch.ts';

test('proxy environments do not pay for a failed native fetch first', async () => {
    let directCalls = 0;
    const value = await readGuideSource('https://api.github.com/guide', { Accept: 'application/json' }, {
        environment: { HTTPS_PROXY: 'http://proxy.invalid' },
        direct: async () => { directCalls++; throw new Error('Direct connection blocked'); },
        proxyAware: async () => 'source',
    });
    assert.equal(value, 'source');
    assert.equal(directCalls, 0);
});

test('unproxied environments use native fetch and recover from a failed connection', async () => {
    let proxyCalls = 0;
    const proxyAware = async () => { proxyCalls++; return 'fallback'; };
    assert.equal(await readGuideSource('https://api.github.com/guide', {}, {
        environment: {}, direct: async () => new Response('direct'), proxyAware,
    }), 'direct');
    assert.equal(proxyCalls, 0);
    assert.equal(await readGuideSource('https://api.github.com/guide', {}, {
        environment: {}, direct: async () => { throw new Error('Network failure'); }, proxyAware,
    }), 'fallback');
    assert.equal(proxyCalls, 1);
});

test('guide source fetch rejects untrusted hosts and does not follow redirects', async () => {
    let directCalls = 0;
    await assert.rejects(readGuideSource('https://example.org/guide', {}, {
        environment: {},
        direct: async () => { directCalls++; return new Response('unexpected'); },
        proxyAware: async () => 'unexpected',
    }), /official GitHub API host/);
    assert.equal(directCalls, 0);

    await assert.rejects(readGuideSource('https://api.github.com:444/guide', {}, {
        environment: {},
        direct: async () => { directCalls++; return new Response('unexpected'); },
        proxyAware: async () => 'unexpected',
    }), /official GitHub API host/);
    assert.equal(directCalls, 0);

    await assert.rejects(readGuideSource('https://api.github.com/guide', {}, {
        environment: {},
        direct: async (_url, init) => {
            assert.equal(init?.redirect, 'manual');
            return new Response(null, { status: 302, headers: { location: 'http://169.254.169.254/' } });
        },
        proxyAware: async () => { throw new Error('redirect rejected'); },
    }), /Official llm-d guide request failed \(302\)/);
});
