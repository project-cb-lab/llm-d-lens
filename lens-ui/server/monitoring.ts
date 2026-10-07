// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import { Router } from 'express';
import { sameOriginUrl } from './sameOriginUrl.ts';

export const monitoringRouter = Router();

monitoringRouter.use('/api/v1/monitoring', async (req, res) => {
    const upstreamBase = process.env.MONITORING_API_URL
        || process.env.SIMULATION_API_URL
        || process.env.OPTIMALBENCH_URL
        || 'http://127.0.0.1:8081';
    let upstreamUrl: URL;
    try {
        upstreamUrl = sameOriginUrl(req.originalUrl, upstreamBase);
    } catch {
        return res.status(400).json({ detail: 'The request URL is not valid' });
    }
    const configuredTimeout = Number(process.env.MONITORING_PROXY_TIMEOUT_MS || 60_000);
    const defaultTimeout = Number.isFinite(configuredTimeout) && configuredTimeout > 0 ? configuredTimeout : 60_000;
    // Driver installation is synchronous and can legitimately take longer than a
    // read (hardware-presence wait + NFD/driver apply), so it gets its own budget.
    const isInstall = /\/gpu-driver\/install$/.test(new URL(req.originalUrl, 'http://localhost').pathname);
    const configuredInstallTimeout = Number(process.env.MONITORING_INSTALL_TIMEOUT_MS || 600_000);
    const installTimeout = Number.isFinite(configuredInstallTimeout) && configuredInstallTimeout > 0
        ? configuredInstallTimeout
        : 600_000;
    const timeout = isInstall ? installTimeout : defaultTimeout;
    const headers = new Headers({ accept: 'application/json' });
    const idempotencyKey = req.header('Idempotency-Key');
    if (idempotencyKey) headers.set('Idempotency-Key', idempotencyKey);
    if (req.headers.cookie) headers.set('cookie', req.headers.cookie);
    if (req.headers.authorization) headers.set('authorization', req.headers.authorization);
    if (req.headers['x-prism-csrf']) headers.set('x-prism-csrf', req.headers['x-prism-csrf'] as string);
    let body: string | undefined;
    if (!['GET', 'HEAD'].includes(req.method)) {
        headers.set('content-type', 'application/json');
        body = JSON.stringify(req.body ?? {});
    }
    try {
        const upstream = await fetch(upstreamUrl, {
            method: req.method,
            headers,
            body,
            redirect: 'manual',
            signal: AbortSignal.timeout(timeout),
        });
        const text = await upstream.text();
        res.status(upstream.status);
        const contentType = upstream.headers.get('content-type');
        if (contentType) res.setHeader('content-type', contentType);
        const setCookies = typeof upstream.headers.getSetCookie === 'function'
            ? upstream.headers.getSetCookie()
            : [];
        for (const cookie of setCookies) res.append('set-cookie', cookie);
        return res.send(text);
    } catch (error) {
        const message = (error as Error).name === 'TimeoutError'
            ? 'Monitoring request timed out'
            : 'Monitoring service is unavailable';
        return res.status((error as Error).name === 'TimeoutError' ? 504 : 502).json({ detail: message });
    }
});
