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

// Signed Node->Python/loopback identity (design section 8.3): HMAC-SHA256 over
// `ts\nMETHOD\npath\nprincipal_id`, mirroring llm_d_bench.auth.security. The
// principal id is the only identity claim; Python recomputes permissions and
// cluster scope from the database, so a stale Node cache cannot grant access.
//
// An AsyncLocalStorage carries the authenticated caller through the Express
// request so deep helpers (clusterSources, planningDiscovery, the MCP loopback)
// can sign without threading a context parameter through every call site.

import crypto from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';

export const INTERNAL_PRINCIPAL_HEADER = 'x-prism-principal-id';
export const INTERNAL_TS_HEADER = 'x-prism-internal-ts';
export const INTERNAL_SIG_HEADER = 'x-prism-internal-sig';

// Must match INTERNAL_AUTH_WINDOW_SECONDS in llm_d_bench/auth/security.py.
const INTERNAL_AUTH_WINDOW_SECONDS = 60;

export type InternalAuthContext = { principalId: string };

const storage = new AsyncLocalStorage<InternalAuthContext>();

export function internalAuthSecret(): string {
    return process.env.LENS_INTERNAL_AUTH_SECRET || '';
}

export function internalAuthEnabled(): boolean {
    return internalAuthSecret().length > 0;
}

/** Run `fn` so downstream async work can sign as `principalId`. */
export function runWithInternalAuth<T>(principalId: string, fn: () => T): T {
    return storage.run({ principalId }, fn);
}

export function currentInternalPrincipalId(): string | undefined {
    return storage.getStore()?.principalId;
}

function internalMessage(timestamp: number, method: string, path: string, principalId: string): string {
    return `${timestamp}\n${method.toUpperCase()}\n${path}\n${principalId}`;
}

/** Headers asserting `principalId` for one concrete method+path. */
export function signInternalHeaders(method: string, path: string, principalId: string): Record<string, string> {
    const secret = internalAuthSecret();
    if (!secret) return {};
    const timestamp = Math.floor(Date.now() / 1000);
    const signature = crypto
        .createHmac('sha256', secret)
        .update(internalMessage(timestamp, method, path, principalId))
        .digest('hex');
    return {
        [INTERNAL_PRINCIPAL_HEADER]: principalId,
        [INTERNAL_TS_HEADER]: String(timestamp),
        [INTERNAL_SIG_HEADER]: signature,
    };
}

/**
 * Headers for an outbound internal request, signed as the authenticated caller
 * of the current Express request. Empty when unauthenticated or unsigned.
 */
export function internalHeadersFor(method: string, urlOrPath: string): Record<string, string> {
    const principalId = currentInternalPrincipalId();
    if (!principalId || !internalAuthEnabled()) return {};
    const path = new URL(urlOrPath, 'http://localhost').pathname;
    return signInternalHeaders(method, path, principalId);
}

export type IncomingInternalHeaders = {
    principalId?: string;
    timestamp?: string;
    signature?: string;
};

/** Verify a signed assertion; returns the principal id or null (mirrors Python). */
export function verifyInternalHeaders(
    method: string,
    path: string,
    headers: IncomingInternalHeaders,
): string | null {
    const secret = internalAuthSecret();
    const { principalId, timestamp, signature } = headers;
    if (!secret || !principalId || !timestamp || !signature) return null;
    const ts = Number(timestamp);
    if (!Number.isFinite(ts)) return null;
    if (Math.abs(Math.floor(Date.now() / 1000) - ts) > INTERNAL_AUTH_WINDOW_SECONDS) return null;
    const expected = crypto
        .createHmac('sha256', secret)
        .update(internalMessage(ts, method, path, principalId))
        .digest('hex');
    const expectedBuffer = Buffer.from(expected, 'utf8');
    const providedBuffer = Buffer.from(signature, 'utf8');
    if (expectedBuffer.length !== providedBuffer.length) return null;
    if (!crypto.timingSafeEqual(expectedBuffer, providedBuffer)) return null;
    return principalId;
}
