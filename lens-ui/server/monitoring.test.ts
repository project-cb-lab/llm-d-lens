// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import assert from 'node:assert/strict';
import http from 'node:http';
import test, { after, before } from 'node:test';
import express from 'express';

import { monitoringRouter } from './monitoring.ts';

let upstream: http.Server;
let proxy: http.Server;
let proxyUrl = '';
let delayMs = 0;

function listen(server: http.Server): Promise<number> {
    return new Promise((resolve) => {
        server.listen(0, '127.0.0.1', () => resolve((server.address() as { port: number }).port));
    });
}

before(async () => {
    upstream = http.createServer((_req, res) => {
        setTimeout(() => {
            res.writeHead(200, { 'content-type': 'application/json' });
            res.end('{"ok":true}');
        }, delayMs);
    });
    const upstreamPort = await listen(upstream);
    process.env.MONITORING_API_URL = `http://127.0.0.1:${upstreamPort}`;
    process.env.MONITORING_PROXY_TIMEOUT_MS = '100';
    process.env.MONITORING_INSTALL_TIMEOUT_MS = '1000';

    const app = express();
    app.use(express.json());
    app.use(monitoringRouter);
    proxy = http.createServer(app);
    const proxyPort = await listen(proxy);
    proxyUrl = `http://127.0.0.1:${proxyPort}`;
});

after(async () => {
    await new Promise((resolve) => upstream.close(resolve));
    await new Promise((resolve) => proxy.close(resolve));
});

test('read requests use the short monitoring timeout', async () => {
    delayMs = 300;
    const response = await fetch(`${proxyUrl}/api/v1/monitoring/gpu-driver/status?access_mode=dra`);
    assert.equal(response.status, 504);
    assert.deepEqual(await response.json(), { detail: 'Monitoring request timed out' });
});

test('driver install uses the longer install timeout', async () => {
    delayMs = 300;
    const response = await fetch(`${proxyUrl}/api/v1/monitoring/gpu-driver/install`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ access_mode: 'plugin' }),
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ok: true });
});
