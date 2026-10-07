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
import test, { after, before } from 'node:test';
import compression from 'compression';
import express from 'express';

import { backendApiProxyRouter } from './backend-api-proxy.ts';
import { monitoringRouter } from './monitoring.ts';
import { sameOriginUrl } from './sameOriginUrl.ts';

type UpstreamCall = { method: string; url: string; body: string };

let upstream: http.Server;
let proxy: http.Server;
let proxyUrl = '';
let calls: UpstreamCall[] = [];
const defaultResponder = (req: http.IncomingMessage, res: http.ServerResponse) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ method: req.method }));
};
let respond = defaultResponder;

test('configured proxy URLs cannot be replaced with a request-controlled origin', () => {
    assert.equal(
        sameOriginUrl('/api/v1/deployments?limit=10', 'http://backend.internal:8081').href,
        'http://backend.internal:8081/api/v1/deployments?limit=10',
    );
    assert.throws(() => sameOriginUrl('//attacker.example/path', 'https://backend.example'), /configured origin/);
    assert.throws(() => sameOriginUrl('/api/v1/deployments', 'file:///tmp/backend'), /HTTP\(S\)/);
});

test('same-origin proxy URLs encode path and query data without changing parameter semantics', () => {
    const resolved = sameOriginUrl('/api/v1/models/a%2Fb?name=hello+world&name=two%26three', 'http://backend.internal:8081');
    assert.equal(resolved.origin, 'http://backend.internal:8081');
    assert.equal(resolved.pathname, '/api/v1/models/a%2Fb');
    assert.deepEqual([...resolved.searchParams], [['name', 'hello world'], ['name', 'two&three']]);
});

function listen(server: http.Server): Promise<number> {
    return new Promise((resolve) => {
        server.listen(0, '127.0.0.1', () => resolve((server.address() as { port: number }).port));
    });
}

before(async () => {
    upstream = http.createServer((req, res) => {
        const chunks: Buffer[] = [];
        req.on('data', (chunk) => chunks.push(chunk as Buffer));
        req.on('end', () => {
            calls.push({ method: req.method || '', url: req.url || '', body: Buffer.concat(chunks).toString() });
            respond(req, res);
        });
    });
    const upstreamPort = await listen(upstream);
    process.env.LLM_D_DEPLOY_API_URL = `http://127.0.0.1:${upstreamPort}`;
    process.env.SIMULATION_API_URL = `http://127.0.0.1:${upstreamPort}`;

    const app = express();
    app.use(compression({
        filter: (req, res) => !/^\/api\/agentic-deployments(?:\/[^/]+\/refine)?\/stream$/.test(req.path) && compression.filter(req, res),
    }));
    app.use(express.json());
    app.use(backendApiProxyRouter);
    app.use(monitoringRouter);
    proxy = http.createServer(app);
    const proxyPort = await listen(proxy);
    proxyUrl = `http://127.0.0.1:${proxyPort}`;
});

after(async () => {
    await new Promise((resolve) => upstream.close(resolve));
    await new Promise((resolve) => proxy.close(resolve));
});

test('malformed proxy paths return 400 without reaching the upstream', async () => {
    calls = [];
    for (const path of ['/api/v1/deployments/%FF', '/api/v1/monitoring/%FF']) {
        const response = await fetch(`${proxyUrl}${path}`);
        assert.equal(response.status, 400, path);
    }
    assert.equal(calls.length, 0);
});

test('deployment list query parameters reach the upstream unchanged', async () => {
    calls = [];
    respond = (_req, res) => {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ items: [] }));
    };

    const response = await fetch(`${proxyUrl}/api/v1/deployments/executions?query=qwen&statuses=ready&statuses=failed&limit=50`);

    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { items: [] });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, 'GET');
    assert.equal(calls[0].url, '/api/v1/deployments/executions?query=qwen&statuses=ready&statuses=failed&limit=50');
});

test('AIConfigurator support POST is forwarded to the upstream backend', async () => {
    calls = [];
    respond = (_req, res) => {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ supported: true }));
    };

    const response = await fetch(`${proxyUrl}/api/v1/aic/support`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ model_name: 'Qwen/Qwen3-0.6B', gpu_count: 1 }),
    });

    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { supported: true });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, 'POST');
    assert.equal(calls[0].url, '/api/v1/aic/support');
    assert.deepEqual(JSON.parse(calls[0].body), { model_name: 'Qwen/Qwen3-0.6B', gpu_count: 1 });
});

test('hardware capabilities GET is forwarded to the upstream backend', async () => {
    calls = [];
    respond = (_req, res) => {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ version: 'v1', profiles: [{ id: 'intel-xpu' }] }));
    };

    const response = await fetch(`${proxyUrl}/api/v1/hardware/capabilities`);

    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { version: 'v1', profiles: [{ id: 'intel-xpu' }] });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, 'GET');
    assert.equal(calls[0].url, '/api/v1/hardware/capabilities');
});

test('deployment metadata PATCH forwards the JSON body', async () => {
    calls = [];
    respond = (_req, res) => {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ execution_id: 'exec-1', display_name: 'Prod', description: 'Serving traffic' }));
    };

    const response = await fetch(`${proxyUrl}/api/v1/deployments/executions/exec-1`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ display_name: 'Prod', description: 'Serving traffic' }),
    });

    assert.equal(response.status, 200);
    assert.equal(calls[0].method, 'PATCH');
    assert.equal(calls[0].url, '/api/v1/deployments/executions/exec-1');
    assert.deepEqual(JSON.parse(calls[0].body), { display_name: 'Prod', description: 'Serving traffic' });
});

test('deployment DELETE passes an empty 204 response through', async () => {
    calls = [];
    respond = (_req, res) => {
        res.writeHead(204);
        res.end();
    };

    const response = await fetch(`${proxyUrl}/api/v1/deployments/executions/exec-1`, { method: 'DELETE' });

    assert.equal(response.status, 204);
    assert.equal(await response.text(), '');
    assert.equal(calls[0].method, 'DELETE');
    assert.equal(calls[0].url, '/api/v1/deployments/executions/exec-1');
});

test('deployment problem+json errors keep their status, content type and code', async () => {
    calls = [];
    respond = (_req, res) => {
        res.writeHead(409, { 'content-type': 'application/problem+json' });
        res.end(JSON.stringify({
            type: 'about:blank',
            status: 409,
            title: 'Deployment in use',
            detail: 'deployment is in use by an active evaluation',
            code: 'deployment_in_use',
        }));
    };

    const response = await fetch(`${proxyUrl}/api/v1/deployments/executions/exec-1`, { method: 'DELETE' });

    assert.equal(response.status, 409);
    assert.match(response.headers.get('content-type') || '', /application\/problem\+json/);
    const payload = await response.json();
    assert.equal(payload.code, 'deployment_in_use');
    assert.equal(payload.detail, 'deployment is in use by an active evaluation');
});

test('Agentic SSE responses bypass compression and preserve streamed events', async () => {
    let finishStream: (() => void) | undefined;
    respond = (_req, res) => {
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        res.write('event: progress\ndata: {"phase":"generation"}\n\n');
        finishStream = () => res.end('event: complete\ndata: {"id":"run-1"}\n\n');
    };

    const response = await fetch(`${proxyUrl}/api/agentic-deployments/stream`, {
        method: 'POST',
        headers: { accept: 'text/event-stream', 'content-type': 'application/json' },
        body: JSON.stringify({ model: 'Qwen/test' }),
    });

    assert.equal(response.headers.get('content-encoding'), 'identity');
    assert.equal(response.headers.get('x-accel-buffering'), 'no');
    const reader = response.body!.getReader();
    const first = await reader.read();
    assert.match(new TextDecoder().decode(first.value), /event: progress/);
    finishStream!();
    const second = await reader.read();
    assert.match(new TextDecoder().decode(second.value), /event: complete/);
});
