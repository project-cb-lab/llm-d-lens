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

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { prismTools } from './tools.ts';

// Builds a fresh MCP server instance with the Phase 1 read-only tool catalog
// registered. Called once per request in stateless mode (see router.ts) so
// Prism's MCP endpoint stays horizontally scalable and holds no session state
// of its own — every tool re-reads live data from Prism's REST API.
export function createPrismMcpServer(): McpServer {
    const server = new McpServer({ name: 'prism-mcp', version: '0.1.0' });

    // Guard against duplicate tool names (e.g. a stale/hand-edited tools.ts,
    // or a generated tool colliding with a hand-written special tool). A raw
    // McpServer.registerTool() call throws on a duplicate name, and since this
    // server is rebuilt fresh on every request (stateless mode), an unguarded
    // throw here would turn into a -32603 Internal server error on EVERY
    // Playground request, for every user, until someone reads the server log.
    // Skipping the duplicate and logging instead keeps the endpoint usable.
    const seenNames = new Set<string>();

    for (const tool of prismTools) {
        if (seenNames.has(tool.name)) {
            console.error(`[Prism MCP] Skipping duplicate tool registration: ${tool.name} (check server/mcp/tools.ts for a name collision)`);
            continue;
        }
        seenNames.add(tool.name);

        server.registerTool(
            tool.name,
            {
                description: `[${tool.riskTier}] ${tool.description}`,
                inputSchema: tool.inputShape,
            },
            async (args) => {
                try {
                    const result = await tool.handler(args as Record<string, unknown>);
                    return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
                } catch (error) {
                    const message = error instanceof Error ? error.message : String(error);
                    return { content: [{ type: 'text', text: JSON.stringify({ error: message }) }], isError: true };
                }
            },
        );
    }

    return server;
}
