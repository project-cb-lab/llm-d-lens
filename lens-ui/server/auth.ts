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

// Browser-facing authentication gateway (design section 9). Node is the
// primary enforcement point: it resolves the session with the Python auth
// service, protects Node-native routes with a permission map, and strips
// client-supplied internal identity headers before forwarding.

import type { NextFunction, Request, Response } from 'express';

import {
    INTERNAL_PRINCIPAL_HEADER,
    INTERNAL_SIG_HEADER,
    INTERNAL_TS_HEADER,
    internalAuthEnabled,
    runWithInternalAuth,
    signInternalHeaders,
    verifyInternalHeaders,
} from './internalAuth.ts';

export type Principal = {
    userId: string;
    username: string;
    permissions: string[];
    reachableClusters: string[] | null;
    mustChangePassword: boolean;
};

export const PRINCIPAL_HEADER = 'x-prism-principal-id';
const SPOOFABLE_INTERNAL_HEADERS = [
    'x-prism-principal-id',
    'x-prism-principal-name',
    'x-prism-internal-ts',
    'x-prism-internal-sig',
];

// Public API paths: authentication entry points and read-only runtime config.
const PUBLIC_API_PATHS = new Set([
    '/api/health',
    '/healthz',
    '/api/config',
    '/api/v1/auth/login',
    '/api/v1/auth/logout',
    '/api/v1/auth/providers',
    '/api/v1/auth/bootstrap',
]);

// `permission` is a single code, or several codes the caller may hold any of.
export type NodeRoutePermission = { method: string; pattern: RegExp; permission: string | string[] };

// Permissions for routes Node serves itself (everything else is enforced by
// Python after being proxied). Regexes are matched against req.path.
export const NODE_ROUTE_PERMISSIONS: NodeRoutePermission[] = [
    { method: 'GET', pattern: /^\/api\/deploy-poc\/(config|status|validate)$/, permission: 'deploy-poc:job:read' },
    { method: 'POST', pattern: /^\/api\/deploy-poc\/start$/, permission: 'deploy-poc:job:execute' },
    { method: 'POST', pattern: /^\/api\/deploy-poc\/teardown$/, permission: 'deploy-poc:job:teardown' },
    { method: 'GET', pattern: /^\/api\/remote-deploy\/defaults$/, permission: 'remote-deploy:target:read' },
    { method: 'POST', pattern: /^\/api\/remote-deploy\/(fingerprint|test)$/, permission: 'remote-deploy:target:connect' },
    { method: 'POST', pattern: /^\/api\/remote-deploy\/(start|status|logs)$/, permission: 'remote-deploy:deploy:execute' },
    { method: 'POST', pattern: /^\/api\/remote-deploy\/teardown$/, permission: 'remote-deploy:deploy:teardown' },
    { method: 'GET', pattern: /^\/api\/guide-planning\/catalog$/, permission: 'guide:plan:execute' },
    { method: 'POST', pattern: /^\/api\/guide-planning\/(plan|prepare)$/, permission: 'guide:plan:execute' },
    { method: 'POST', pattern: /^\/api\/candidate-(support|search)$/, permission: 'candidate:candidate:search' },
    { method: 'POST', pattern: /^\/api\/configurations\/(resolve|render)$/, permission: 'configuration:artifact:render' },
    { method: 'POST', pattern: /^\/api\/configurations\/save$/, permission: 'configuration:artifact:save' },
    { method: 'POST', pattern: /^\/api\/playground\/chat$/, permission: 'playground:chat:use' },
    // The Playground assistant reaches the MCP server over loopback as the
    // signed-in user, so it may call MCP with `playground:chat:use`; every tool
    // action is still enforced by its own target route permission.
    { method: 'POST', pattern: /^\/api\/mcp$/, permission: ['mcp:tool:invoke', 'playground:chat:use'] },
];

export function backendAuthBase(): string {
    return (
        process.env.LLM_D_DEPLOY_API_URL ||
        process.env.SIMULATION_API_URL ||
        process.env.OPTIMALBENCH_URL ||
        'http://127.0.0.1:8081'
    );
}

export function authDisabled(): boolean {
    return (
        process.env.PRISM_AUTH_MODE === 'disabled' ||
        process.env.PRISM_ALLOW_UNAUTHENTICATED === 'true' ||
        process.env.SIMULATION_ALLOW_UNAUTHENTICATED === 'true'
    );
}

export function isPublicApiPath(path: string): boolean {
    return PUBLIC_API_PATHS.has(path);
}

export function permissionForNodeRoute(method: string, path: string): string | string[] | null {
    const upper = method.toUpperCase();
    for (const rule of NODE_ROUTE_PERMISSIONS) {
        if (rule.method === upper && rule.pattern.test(path)) return rule.permission;
    }
    return null;
}

export function permissionGranted(granted: string[], required: string): boolean {
    if (granted.includes(required)) return true;
    const [rd, rr, ra] = required.split(':');
    return granted.some((code) => {
        const [gd, gr, ga] = code.split(':');
        return (gd === '*' || gd === rd) && (gr === '*' || gr === rr) && (ga === '*' || ga === ra);
    });
}

/** True when the caller holds any of the accepted permission codes. */
export function permissionAnyGranted(granted: string[], required: string | string[]): boolean {
    const codes = Array.isArray(required) ? required : [required];
    return codes.some((code) => permissionGranted(granted, code));
}

function problem(res: Response, status: number, title: string, detail: string, code: string): void {
    res.status(status).type('application/problem+json').json({
        type: 'about:blank',
        status,
        title,
        detail,
        code,
    });
}

/** Resolve the caller's principal through the Python auth service. */
export async function fetchPrincipal(req: Request): Promise<Principal | null> {
    const headers = new Headers({ accept: 'application/json' });
    if (req.headers.cookie) headers.set('cookie', req.headers.cookie);
    if (req.headers.authorization) headers.set('authorization', req.headers.authorization);
    const timeout = Number(process.env.PRISM_AUTH_INTROSPECT_TIMEOUT_MS || 10_000);
    try {
        const response = await fetch(new URL('/api/v1/auth/session', backendAuthBase()), {
            headers,
            signal: AbortSignal.timeout(Number.isFinite(timeout) ? timeout : 10_000),
        });
        if (!response.ok) return null;
        return (await response.json()) as Principal;
    } catch {
        return null;
    }
}

export function stripInternalHeaders(req: Request): void {
    for (const header of SPOOFABLE_INTERNAL_HEADERS) {
        delete req.headers[header];
    }
}

/** Resolve a principal from a verified internal assertion (design section 8.3). */
async function fetchInternalPrincipal(principalId: string): Promise<Principal | null> {
    const path = '/api/v1/auth/session';
    const headers = new Headers({ accept: 'application/json' });
    for (const [name, value] of Object.entries(signInternalHeaders('GET', path, principalId))) {
        headers.set(name, value);
    }
    const timeout = Number(process.env.PRISM_AUTH_INTROSPECT_TIMEOUT_MS || 10_000);
    try {
        const response = await fetch(new URL(path, backendAuthBase()), {
            headers,
            signal: AbortSignal.timeout(Number.isFinite(timeout) ? timeout : 10_000),
        });
        if (!response.ok) return null;
        return (await response.json()) as Principal;
    } catch {
        return null;
    }
}

/** Express middleware: authenticate `/api`, then authorize Node-native routes. */
export async function authMiddleware(req: Request, res: Response, next: NextFunction): Promise<void> {
    if (!req.path.startsWith('/api')) {
        next();
        return;
    }
    // Client-supplied internal headers are only trusted when they carry a valid
    // signature for this exact method+path; otherwise they are stripped so they
    // can never spoof an identity (design section 9.3).
    const signedPrincipalId = internalAuthEnabled()
        ? verifyInternalHeaders(req.method, req.path, {
            principalId: req.header(INTERNAL_PRINCIPAL_HEADER) || undefined,
            timestamp: req.header(INTERNAL_TS_HEADER) || undefined,
            signature: req.header(INTERNAL_SIG_HEADER) || undefined,
        })
        : null;
    if (!signedPrincipalId) {
        stripInternalHeaders(req);
    }
    if (authDisabled() || isPublicApiPath(req.path)) {
        next();
        return;
    }
    const principal = signedPrincipalId
        ? await fetchInternalPrincipal(signedPrincipalId)
        : await fetchPrincipal(req);
    if (!principal) {
        problem(res, 401, 'Unauthenticated', 'A valid session is required', 'unauthenticated');
        return;
    }
    (req as Request & { principal?: Principal }).principal = principal;
    const required = permissionForNodeRoute(req.method, req.path);
    if (required && !permissionAnyGranted(principal.permissions, required)) {
        const label = Array.isArray(required) ? required.join(' or ') : required;
        problem(res, 403, 'Forbidden', `Missing permission: ${label}`, 'forbidden');
        return;
    }
    // Propagate the authenticated caller so deep helpers can sign the
    // Node->Python internal reads they perform on this caller's behalf.
    runWithInternalAuth(principal.userId, () => next());
}

export function principalOf(req: Request): Principal | undefined {
    return (req as Request & { principal?: Principal }).principal;
}
