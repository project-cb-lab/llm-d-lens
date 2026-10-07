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
// Mounts the Prism MCP tool catalog as a stateless Streamable HTTP MCP server
// at POST /api/mcp, per the SDK's stateless pattern: one ephemeral McpServer +
// transport per request, no session id required. This is intentional for
// Phase 1 (read-only tools only) and lets both Prism's own Playground chat
// orchestrator (server/playground/chat.ts) AND any external MCP client
// (MCP Inspector, an IDE agent, etc.) connect the same way.
// -----------------------------------------------------------------------------

import { Router } from 'express';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { createPrismMcpServer } from './server.ts';

export const mcpRouter = Router();

// Note: req.body is already parsed by the global express.json() middleware
// applied in server/server.js before this router is mounted.
mcpRouter.post('/api/mcp', async (req, res) => {
    try {
        const server = createPrismMcpServer();
        const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
        res.on('close', () => {
            transport.close();
            server.close();
        });
        await server.connect(transport);
        await transport.handleRequest(req, res, req.body);
    } catch (error) {
        console.error('[Prism MCP]', error);
        if (!res.headersSent) {
            res.status(500).json({ jsonrpc: '2.0', error: { code: -32603, message: 'Internal server error' }, id: null });
        }
    }
});

// Stateless mode does not support the GET (server->client stream) or DELETE
// (session termination) methods of the Streamable HTTP transport.
mcpRouter.get('/api/mcp', (_req, res) => {
    res.status(405).json({ jsonrpc: '2.0', error: { code: -32000, message: 'Method not allowed.' }, id: null });
});
mcpRouter.delete('/api/mcp', (_req, res) => {
    res.status(405).json({ jsonrpc: '2.0', error: { code: -32000, message: 'Method not allowed.' }, id: null });
});
