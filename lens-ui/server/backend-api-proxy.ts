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

import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { Response, Router } from 'express';
import { principalOf } from './auth.ts';
import { clusterSessionKubeconfig } from './clusterSession.ts';
import { internalAuthEnabled, signInternalHeaders } from './internalAuth.ts';
import { sameOriginUrl } from './sameOriginUrl.ts';

export const backendApiProxyRouter = Router();

function problem(res: Response, status: number, title: string, detail: string, code: string) {
    return res.status(status).type('application/problem+json').json({ type: 'about:blank', status, title, detail, code });
}

// Forwards requests for every backend-served module (simulation, cluster, configurations,
// deployments, evaluate, storage, model cache, AI providers, ...) to the Python
// llm_d_bench API. Not limited to the simulation module despite the historical env var names.
backendApiProxyRouter.use([
    '/api/simulation', '/api/cluster', '/api/cluster-overview', '/api/agentic-deployments',
    '/api/v1/configurations', '/api/v1/cluster', '/api/v1/simulation', '/api/v1/deployments', '/api/v1/evaluate',
    '/api/v1/storage',
    '/api/v1/capacity',
    '/api/v1/model-cache',
    '/api/v1/model-service',
    '/api/v1/ai-providers',
    '/api/v1/aic',
    '/api/v1/hardware',
    '/api/v1/system',
    '/api/v1/versions',
    // auth domain
    '/api/v1/auth',
    '/api/v1/users',
    '/api/v1/groups',
    '/api/v1/roles',
    '/api/v1/identity-providers',
    '/api/v1/audit-logs',
    '/api/v1/sessions',
    '/api/v1/clusters',
    '/api/v1/executions',
], async (req, res) => {
    const canonicalPath = req.originalUrl
        .replace(/^\/api\/v1\/cluster(?=\/|\?|$)/, '/api/cluster')
        .replace(/^\/api\/v1\/simulation(?=\/|\?|$)/, '/api/simulation');
    const upstreamBase = canonicalPath.startsWith('/api/v1/')
        ? process.env.LLM_D_DEPLOY_API_URL
            || process.env.SIMULATION_API_URL
            || process.env.OPTIMALBENCH_URL
            || 'http://127.0.0.1:8081'
        : process.env.SIMULATION_API_URL
            || process.env.OPTIMALBENCH_URL
            || 'http://127.0.0.1:8081';
    let upstreamUrl: URL;
    try {
        upstreamUrl = sameOriginUrl(canonicalPath, upstreamBase);
    } catch {
        return problem(res, 400, 'Invalid upstream path', 'The request URL is not valid', 'invalid_upstream_path');
    }
    const configuredTimeout = Number(process.env.BACKEND_PROXY_TIMEOUT_MS || 3_600_000);
    const timeout = Number.isFinite(configuredTimeout) && configuredTimeout > 0 ? configuredTimeout : 3_600_000;
    const headers = new Headers({ accept: req.header('accept') || 'application/json' });
    // Authentication context must reach the Python API so it can enforce
    // independently of Node (design sections 9.3/8.3).
    if (req.headers.cookie) headers.set('cookie', req.headers.cookie);
    if (req.headers.authorization) headers.set('authorization', req.headers.authorization);
    // CSRF double-submit header must reach Python alongside the cookie.
    if (req.headers['x-prism-csrf']) headers.set('x-prism-csrf', req.headers['x-prism-csrf'] as string);
    // Re-assert the resolved caller with a fresh signature for the canonical
    // upstream path. This carries identity across the /api/v1 -> /api rewrite
    // and lets Python authenticate without relying on the browser cookie.
    const principalId = principalOf(req)?.userId;
    if (internalAuthEnabled() && principalId) {
        const upstreamPath = new URL(canonicalPath, 'http://localhost').pathname;
        for (const [name, value] of Object.entries(signInternalHeaders(req.method, upstreamPath, principalId))) {
            headers.set(name, value);
        }
    }
    let body: string | ReadableStream | undefined;
    if (!['GET', 'HEAD'].includes(req.method)) {
        const contentType = req.header('content-type') || '';
        if (contentType.startsWith('multipart/form-data')) {
            // Forward raw multipart bodies (e.g. kubeconfig uploads) untouched.
            headers.set('content-type', contentType);
            body = Readable.toWeb(req) as unknown as ReadableStream;
        } else {
            headers.set('content-type', 'application/json');
            const requestBody = req.body ?? {};
            if (req.originalUrl === '/api/v1/deployments/runs' && requestBody?.provenance?.cluster_session_id) {
                if (!clusterSessionKubeconfig(requestBody.provenance.cluster_session_id)) {
                    return problem(res, 409, 'Cluster session unavailable', 'Connect the selected cluster before deploying', 'cluster_session_unavailable');
                }
            }
            body = JSON.stringify(requestBody);
        }
    }
    try {
        const upstream = await fetch(upstreamUrl, {
            method: req.method,
            headers,
            body,
            duplex: 'half',
            redirect: 'manual',
            signal: AbortSignal.timeout(timeout),
        } as RequestInit);
        res.status(upstream.status);
        const contentType = upstream.headers.get('content-type');
        if (contentType) res.setHeader('content-type', contentType);
        // Forward Set-Cookie (login/logout) so the browser stores the session.
        const setCookies = typeof upstream.headers.getSetCookie === 'function'
            ? upstream.headers.getSetCookie()
            : [];
        for (const cookie of setCookies) res.append('set-cookie', cookie);
        if (contentType?.includes('text/event-stream')) {
            // Global Express compression otherwise buffers small SSE chunks until
            // the recommendation finishes, hiding intermediate progress updates.
            res.setHeader('content-encoding', 'identity');
            res.setHeader('cache-control', 'no-cache, no-transform');
            res.setHeader('x-accel-buffering', 'no');
            res.flushHeaders();
        }
        if (!upstream.body) return res.end();
        await pipeline(Readable.fromWeb(upstream.body as never), res);
    } catch (error) {
        const message = (error as Error).name === 'TimeoutError'
            ? 'Backend request timed out'
            : 'Backend service is unavailable';
        if (res.headersSent) {
            res.destroy(error as Error);
            return;
        }
        return problem(res, 502, 'Upstream service unavailable', message, 'upstream_unavailable');
    }
});
