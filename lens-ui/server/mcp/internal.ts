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

// -----------------------------------------------------------------------------
// Prism MCP tools are THIN wrappers: every tool handler calls Prism's own
// existing REST API over loopback HTTP instead of re-implementing cluster,
// session, or auth logic. This keeps the MCP layer a pure adapter, matching
// the "no new business logic" principle in docs/design/mcp-playground-design.zh-CN.md.
// -----------------------------------------------------------------------------

import fs from 'fs';
import { Agent } from 'undici';

import { internalHeadersFor } from '../internalAuth.ts';

const DEFAULT_PORT = 3000;

function tlsEnabled(): boolean {
    return Boolean(process.env.TLS_CERT_FILE && process.env.TLS_KEY_FILE);
}

export function internalBaseUrl(): string {
    const port = Number(process.env.SERVER_PORT) || Number(process.env.PORT) || DEFAULT_PORT;
    const scheme = tlsEnabled() ? 'https' : 'http';
    return (process.env.PRISM_MCP_INTERNAL_BASE_URL || `${scheme}://127.0.0.1:${port}`).replace(/\/$/, '');
}

export interface InternalResult {
    ok: boolean;
    status: number;
    body: unknown;
}

// When the server terminates TLS with a self-signed cert (see server.js),
// Node's fetch (undici) rejects it like any browser would. Loopback calls
// here always target our own hardcoded 127.0.0.1 address (never
// user-controlled), so rather than weakening TLS verification globally
// (NODE_TLS_REJECT_UNAUTHORIZED=0) or adding a second plain-HTTP listener,
// we trust exactly the one certificate this process itself generated/loaded.
let insecureLoopbackAgent: Agent | null = null;
function loopbackDispatcher(): Agent | undefined {
    if (!tlsEnabled()) return undefined;
    if (!insecureLoopbackAgent) {
        insecureLoopbackAgent = new Agent({ connect: { ca: fs.readFileSync(process.env.TLS_CERT_FILE as string) } });
    }
    return insecureLoopbackAgent;
}

// A fetch that trusts our own loopback cert, for any caller that needs to
// pass a custom `fetch` into a third-party client (e.g. the MCP SDK's
// StreamableHTTPClientTransport) instead of going through internalRequest().
export const internalFetch: typeof fetch = (input, init) => {
    const dispatcher = loopbackDispatcher();
    return fetch(input, { ...init, ...(dispatcher ? ({ dispatcher } as Record<string, unknown>) : {}) });
};

export async function internalRequest(path: string, init?: RequestInit): Promise<InternalResult> {
    const url = `${internalBaseUrl()}${path}`;
    try {
        const method = (init?.method || 'GET').toUpperCase();
        // The MCP loopback re-enters Prism as the caller already authenticated
        // at POST /api/mcp, so it carries that caller's signed identity instead
        // of a browser cookie (design sections 8.3/9.2).
        const headers: Record<string, string> = {
            accept: 'application/json',
            ...internalHeadersFor(method, path),
        };
        if (init?.body) headers['content-type'] = 'application/json';
        const res = await internalFetch(url, {
            ...init,
            headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) },
        });
        const text = await res.text();
        let body: unknown = null;
        if (text) {
            try {
                body = JSON.parse(text);
            } catch {
                body = text;
            }
        }
        return { ok: res.ok, status: res.status, body };
    } catch (error) {
        return { ok: false, status: 0, body: { error: error instanceof Error ? error.message : String(error) } };
    }
}

export function buildQuery(params: Record<string, string | number | undefined | null>): string {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
        if (value === undefined || value === null || value === '') continue;
        search.set(key, String(value));
    }
    const query = search.toString();
    return query ? `?${query}` : '';
}
