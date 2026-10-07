// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import assert from 'node:assert/strict';
import http from 'node:http';
import test, { after, before, beforeEach } from 'node:test';
import express from 'express';

import { authMiddleware } from './auth.ts';
import { backendApiProxyRouter } from './backend-api-proxy.ts';
import { signInternalHeaders } from './internalAuth.ts';

const INTERNAL_SECRET = 'test-internal-secret';

let upstream: http.Server;
let gateway: http.Server;
let gatewayUrl = '';
let upstreamRequests: { url: string; cookie?: string; spoofed?: string; csrf?: string; internalSig?: string }[] = [];

function listen(server: http.Server): Promise<number> {
    return new Promise((resolve) => {
        server.listen(0, '127.0.0.1', () => resolve((server.address() as { port: number }).port));
    });
}

before(async () => {
    process.env.LENS_INTERNAL_AUTH_SECRET = INTERNAL_SECRET;
    upstream = http.createServer((req, res) => {
        upstreamRequests.push({
            url: req.url || '',
            cookie: req.headers.cookie,
            spoofed: req.headers['x-prism-principal-id'] as string | undefined,
            csrf: req.headers['x-prism-csrf'] as string | undefined,
            internalSig: req.headers['x-prism-internal-sig'] as string | undefined,
        });
        if ((req.url || '').startsWith('/api/v1/auth/session')) {
            const cookie = req.headers.cookie || '';
            // Node verifies the signature before introspecting, so a signed
            // session call is trusted here even without a browser cookie.
            const permissions = cookie.includes('prism_session=playground')
                ? ['playground:chat:use']
                : cookie.includes('prism_session=good') || req.headers['x-prism-internal-sig']
                    ? ['deploy-poc:job:execute', 'deployment:run:read']
                    : null;
            if (permissions) {
                res.writeHead(200, { 'content-type': 'application/json' });
                res.end(JSON.stringify({
                    userId: 'u1',
                    username: 'alice',
                    permissions,
                    reachableClusters: ['c1'],
                    mustChangePassword: false,
                }));
            } else {
                res.writeHead(401, { 'content-type': 'application/problem+json' });
                res.end(JSON.stringify({ code: 'unauthenticated' }));
            }
            return;
        }
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ items: [] }));
    });
    const upstreamPort = await listen(upstream);
    process.env.LLM_D_DEPLOY_API_URL = `http://127.0.0.1:${upstreamPort}`;

    const app = express();
    app.use(express.json());
    app.use(authMiddleware);
    app.get('/api/config', (_req, res) => res.json({ ok: true }));
    app.post('/api/deploy-poc/start', (req, res) => {
        res.json({ spoofed: req.headers['x-prism-principal-id'] ?? null });
    });
    app.post('/api/mcp', (_req, res) => res.json({ ok: true }));
    app.use(backendApiProxyRouter);
    gateway = http.createServer(app);
    const gatewayPort = await listen(gateway);
    gatewayUrl = `http://127.0.0.1:${gatewayPort}`;
});

after(async () => {
    await new Promise((resolve) => upstream.close(resolve));
    await new Promise((resolve) => gateway.close(resolve));
});

beforeEach(() => {
    upstreamRequests = [];
    delete process.env.PRISM_AUTH_MODE;
    delete process.env.PRISM_ALLOW_UNAUTHENTICATED;
    delete process.env.SIMULATION_ALLOW_UNAUTHENTICATED;
});

test('public config endpoint is reachable without a session', async () => {
    const response = await fetch(`${gatewayUrl}/api/config`);
    assert.equal(response.status, 200);
});

test('protected proxied route rejects an anonymous request', async () => {
    const response = await fetch(`${gatewayUrl}/api/v1/users`);
    assert.equal(response.status, 401);
    const body = await response.json();
    assert.equal(body.code, 'unauthenticated');
});

test('valid session is forwarded to the Python API', async () => {
    const response = await fetch(`${gatewayUrl}/api/v1/users`, {
        headers: { cookie: 'prism_session=good' },
    });
    assert.equal(response.status, 200);
    const forwarded = upstreamRequests.find((entry) => entry.url.startsWith('/api/v1/users'));
    assert.ok(forwarded);
    assert.match(forwarded?.cookie || '', /prism_session=good/);
});

test('csrf header is forwarded to the Python API', async () => {
    const response = await fetch(`${gatewayUrl}/api/v1/users`, {
        method: 'POST',
        headers: {
            cookie: 'prism_session=good',
            'content-type': 'application/json',
            'x-prism-csrf': 'csrf-value-123',
        },
        body: '{}',
    });
    assert.equal(response.status, 200);
    const forwarded = upstreamRequests.find((entry) => entry.url.startsWith('/api/v1/users'));
    assert.ok(forwarded);
    assert.equal(forwarded?.csrf, 'csrf-value-123');
});


test('node-native route enforces its permission', async () => {
    // The principal has deploy-poc:job:execute, so it succeeds.
    const allowed = await fetch(`${gatewayUrl}/api/deploy-poc/start`, {
        method: 'POST',
        headers: { cookie: 'prism_session=good', 'content-type': 'application/json' },
        body: '{}',
    });
    assert.equal(allowed.status, 200);
    assert.equal((await allowed.json()).spoofed, null);
});

test('missing session rejects a node-native route', async () => {
    const response = await fetch(`${gatewayUrl}/api/deploy-poc/start`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: '{}',
    });
    assert.equal(response.status, 401);
});

test('spoofed internal principal headers are stripped', async () => {
    const response = await fetch(`${gatewayUrl}/api/deploy-poc/start`, {
        method: 'POST',
        headers: {
            cookie: 'prism_session=good',
            'content-type': 'application/json',
            'x-prism-principal-id': 'attacker',
        },
        body: '{}',
    });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).spoofed, null);
});

test('valid internal signature authenticates without a cookie', async () => {
    const response = await fetch(`${gatewayUrl}/api/v1/users`, {
        headers: signInternalHeaders('GET', '/api/v1/users', 'u2'),
    });
    assert.equal(response.status, 200);
    const forwarded = upstreamRequests.find((entry) => entry.url.startsWith('/api/v1/users'));
    assert.ok(forwarded);
    assert.ok(forwarded?.internalSig);
});

test('tampered internal signature is rejected', async () => {
    const headers = signInternalHeaders('GET', '/api/v1/users', 'u2');
    headers['x-prism-internal-sig'] = 'deadbeef';
    const response = await fetch(`${gatewayUrl}/api/v1/users`, { headers });
    assert.equal(response.status, 401);
});

test('internal signature is scoped to its method and path', async () => {
    const response = await fetch(`${gatewayUrl}/api/v1/users`, {
        headers: signInternalHeaders('GET', '/api/v1/groups', 'u2'),
    });
    assert.equal(response.status, 401);
});

test('mcp accepts the playground permission for the assistant loopback', async () => {
    const response = await fetch(`${gatewayUrl}/api/mcp`, {
        method: 'POST',
        headers: { cookie: 'prism_session=playground', 'content-type': 'application/json' },
        body: '{}',
    });
    assert.equal(response.status, 200);
});

test('mcp rejects a principal without mcp:tool:invoke or playground:chat:use', async () => {
    const response = await fetch(`${gatewayUrl}/api/mcp`, {
        method: 'POST',
        headers: { cookie: 'prism_session=good', 'content-type': 'application/json' },
        body: '{}',
    });
    assert.equal(response.status, 403);
    const body = await response.json();
    assert.match(body.detail, /mcp:tool:invoke or playground:chat:use/);
});

test('disabled auth mode bypasses authentication', async () => {
    process.env.PRISM_AUTH_MODE = 'disabled';
    const response = await fetch(`${gatewayUrl}/api/v1/users`);
    assert.equal(response.status, 200);
});
