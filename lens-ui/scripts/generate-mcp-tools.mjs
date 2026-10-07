#!/usr/bin/env node
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
// Regenerates server/mcp/tools.ts -- entirely -- from the llm_d_bench FastAPI
// app's own OpenAPI schema, so a Lens MCP tool for a REST endpoint stays in
// sync with that endpoint's real, current shape without anyone hand-copying
// request/response fields into this file.
//
// tools.ts is an ignored build artifact. Generate it for local development;
// the Dockerfile generates it in a Python/Node build stage for production.
// `--check` compares an existing generated catalog with current API routes.
//
// Hand-written tools that are NOT a 1:1 wrapper over one FastAPI operation
// (composite/custom logic, or backed by a Node/Express route instead of the
// FastAPI app -- e.g. Deploy PoC, remote-deploy, guide planning,
// candidate search, wait_for_status) live in server/mcp/specialTools.ts
// instead, and are never touched by this script. Edit that file directly.
//
// Usage:  node scripts/generate-mcp-tools.mjs [--check]
//   (no flags)  Regenerates server/mcp/tools.ts in place.
//   --check     Regenerates into memory only and exits non-zero if the file
//               would change (for CI: "did someone forget to regenerate?").
//
// How a route becomes a tool (or doesn't):
//   - riskTier is derived from the HTTP method by default: GET/HEAD -> read,
//     POST/PUT/PATCH -> write, DELETE -> approve (see docs/design/mcp-playground-design.zh-CN.md)
//     -- no attempt is made to infer risk from the route's real side effects.
//     RISK_TIER_OVERRIDES below is a small, explicit, named exception list
//     for the rare operationId that's known to kick off a real side effect
//     despite its HTTP method (e.g. approve_agentic_plan actually starts a
//     deployment even though it's a POST) -- not a general inference engine.
//   - The tool's name comes from the route's OpenAPI operationId, and its
//     description from the route's OpenAPI `description`. BOTH `summary` and
//     `description` must be set on the FastAPI route (e.g.
//     @router.get(..., summary=..., description=..., operation_id=...)) or
//     the route is skipped with a warning -- we never invent a description,
//     since that's exactly the LLM-facing text that determines whether the
//     model uses the tool correctly.
// -----------------------------------------------------------------------------

import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..');
const TOOLS_TS_PATH = path.join(REPO_ROOT, 'server/mcp/tools.ts');
const SPECIAL_TOOLS_TS_PATH = path.join(REPO_ROOT, 'server/mcp/specialTools.ts');
const PYTHON_BIN = process.env.PRISM_PYTHON_BIN || path.join(REPO_ROOT, '.venv/bin/python3');

const METHOD_TO_RISK_TIER = {
    get: 'read',
    head: 'read',
    post: 'write',
    put: 'write',
    patch: 'write',
    delete: 'approve',
};
// Narrow, explicit exceptions to the method-derived riskTier above, for
// operations whose operationId is known to kick off a real, hard-to-undo
// side effect (e.g. actually starting a deployment) despite being a POST.
// Kept as a tiny, named allowlist -- NOT a general risk-inference mechanism
// -- so it stays easy to audit; mirrors the same manual 'approve' tier
// already used for hand-written tools like start_deploy_poc/
// start_remote_deploy in specialTools.ts.
const RISK_TIER_OVERRIDES = {
    // Begins actually executing an Agentic Deployment plan (provisioning the
    // selected candidate) -- must pause for human confirmation like any
    // other "start a deployment" action, not auto-run as a plain write.
    approve_agentic_plan: 'approve',
};
const JSON_BODY_METHODS = new Set(['post', 'put', 'patch', 'delete']);

function dumpOpenApiSchema() {
    const output = execFileSync(
        PYTHON_BIN,
        ['-c', 'import json; from llm_d_bench.api.main import app; print(json.dumps(app.openapi()))'],
        { cwd: REPO_ROOT, maxBuffer: 64 * 1024 * 1024 },
    );
    return JSON.parse(output.toString('utf8'));
}

function resolveRef(schema, components) {
    if (schema && typeof schema === 'object' && schema.$ref) {
        const refName = schema.$ref.split('/').pop();
        const resolved = components.schemas?.[refName];
        if (!resolved) throw new Error(`Unresolvable $ref: ${schema.$ref}`);
        return resolved;
    }
    return schema;
}

// Converts a JSON Schema (already $ref-resolved) node into zod source code
// (a string of TS expression text, e.g. "z.string().optional()"). Kept
// intentionally simple/generic: covers the common OpenAPI/Pydantic shapes
// this codebase actually produces (string/number/integer/boolean/array/
// object/enum, nullable via anyOf-with-null or "nullable: true"), and falls
// back to z.unknown() for anything unusual rather than guessing wrong.
function jsonSchemaToZod(schema, components, seenRefs = new Set()) {
    if (!schema || typeof schema !== 'object') return 'z.unknown()';

    if (schema.$ref) {
        const refName = schema.$ref.split('/').pop();
        if (seenRefs.has(refName)) return 'z.unknown()'; // cycle guard
        return jsonSchemaToZod(resolveRef(schema, components), components, new Set([...seenRefs, refName]));
    }

    // Pydantic's Optional[X] usually renders as anyOf: [X, {type: null}] (or
    // "nullable: true" alongside a concrete type on older OpenAPI versions).
    const anyOf = schema.anyOf || schema.oneOf;
    if (Array.isArray(anyOf)) {
        const nonNull = anyOf.filter((entry) => entry.type !== 'null');
        if (nonNull.length === 1) {
            const inner = jsonSchemaToZod(nonNull[0], components, seenRefs);
            const isNullable = anyOf.length !== nonNull.length;
            return isNullable ? `${inner}.nullable()` : inner;
        }
        return 'z.unknown()';
    }

    let zodExpr;
    switch (schema.type) {
        case 'string':
            zodExpr = schema.enum ? `z.enum([${schema.enum.map((v) => JSON.stringify(String(v))).join(', ')}])` : 'z.string()';
            break;
        case 'integer':
            zodExpr = 'z.number().int()';
            break;
        case 'number':
            zodExpr = 'z.number()';
            break;
        case 'boolean':
            zodExpr = 'z.boolean()';
            break;
        case 'array': {
            const itemExpr = jsonSchemaToZod(schema.items || {}, components, seenRefs);
            zodExpr = `z.array(${itemExpr})`;
            break;
        }
        case 'object':
        default: {
            if (schema.type === 'object' || schema.properties) {
                const props = schema.properties || {};
                const required = new Set(schema.required || []);
                const fieldLines = Object.entries(props).map(([key, propSchema]) => {
                    let expr = jsonSchemaToZod(propSchema, components, seenRefs);
                    if (!required.has(key)) expr += '.optional()';
                    const description = describeOf(propSchema, components);
                    if (description) expr += `.describe(${JSON.stringify(description)})`;
                    return `${JSON.stringify(key)}: ${expr}`;
                });
                zodExpr = fieldLines.length
                    ? `z.object({ ${fieldLines.join(', ')} }).passthrough()`
                    : 'z.record(z.string(), z.unknown())';
            } else {
                zodExpr = 'z.unknown()';
            }
        }
    }
    if (schema.nullable) zodExpr += '.nullable()';
    return zodExpr;
}

function describeOf(schema, components) {
    const resolved = schema?.$ref ? resolveRef(schema, components) : schema;
    return resolved?.description || undefined;
}

// Builds one PrismTool entry's source text for a single OpenAPI operation
// that has already passed the summary/description/operationId checks.
function buildToolSource(apiPath, method, operation, components) {
    const riskTier = RISK_TIER_OVERRIDES[operation.operationId] || METHOD_TO_RISK_TIER[method];
    const pathParams = (operation.parameters || []).filter((p) => p.in === 'path');
    const queryParams = (operation.parameters || []).filter((p) => p.in === 'query');

    const shapeFields = [];
    const usedNames = new Set();
    for (const param of [...pathParams, ...queryParams]) {
        let expr = jsonSchemaToZod(param.schema || {}, components);
        if (!param.required) expr += '.optional()';
        const description = param.description || param.schema?.description;
        if (description) expr += `.describe(${JSON.stringify(description)})`;
        shapeFields.push(`            ${JSON.stringify(param.name)}: ${expr},`);
        usedNames.add(param.name);
    }

    let bodySchema;
    const requestBodyContent = operation.requestBody?.content?.['application/json'];
    if (requestBodyContent?.schema) {
        bodySchema = resolveRef(requestBodyContent.schema, components);
    }
    const bodyFieldNames = [];
    if (bodySchema?.properties) {
        const required = new Set(bodySchema.required || []);
        for (const [key, propSchema] of Object.entries(bodySchema.properties)) {
            // A body property may share its name with a path/query param (e.g. a path
            // param that is also echoed in the request body); the path/query entry
            // already covers it in inputShape, so just reuse it as the body value too.
            if (!usedNames.has(key)) {
                let expr = jsonSchemaToZod(propSchema, components);
                if (!required.has(key)) expr += '.optional()';
                const description = describeOf(propSchema, components);
                if (description) expr += `.describe(${JSON.stringify(description)})`;
                shapeFields.push(`            ${JSON.stringify(key)}: ${expr},`);
                usedNames.add(key);
            }
            bodyFieldNames.push(key);
        }
    } else if (bodySchema) {
        // Non-object JSON body (rare): accept it opaquely under "body".
        shapeFields.push(`            body: z.unknown().describe('Request body for this operation.'),`);
        bodyFieldNames.push('body');
    }

    // Path template: {param} -> ${encodeURIComponent(String(args.param))}.
    let pathTemplate = apiPath.replace(/\{([^}]+)\}/g, (_m, name) => `\${encodeURIComponent(String(args[${JSON.stringify(name)}]))}`);
    const usesTemplateLiteral = pathTemplate.includes('${');
    const pathExpr = usesTemplateLiteral ? `\`${pathTemplate}\`` : JSON.stringify(pathTemplate);

    const remainingQueryNames = queryParams.map((p) => p.name);
    const queryExpr = remainingQueryNames.length
        ? ` + buildQuery({ ${remainingQueryNames.map((n) => `${JSON.stringify(n)}: args[${JSON.stringify(n)}] as string | number | undefined`).join(', ')} })`
        : '';

    let handlerBody;
    if (JSON_BODY_METHODS.has(method)) {
        const bodyExpr = bodyFieldNames.length
            ? `{ ${bodyFieldNames.map((n) => `${JSON.stringify(n)}: args[${JSON.stringify(n)}]`).join(', ')} }`
            : 'undefined';
        handlerBody = `jsonRequest(${pathExpr}${queryExpr}, ${JSON.stringify(method.toUpperCase())}, ${bodyExpr})`;
    } else {
        handlerBody = `toPlainResult(await internalRequest(${pathExpr}${queryExpr}))`;
    }

    const name = operation.operationId;
    const description = operation.description;
    const usesArgs = handlerBody.includes('args[');
    const paramList = usesArgs ? 'args' : '';

    return [
        '    {',
        `        name: ${JSON.stringify(name)},`,
        `        description: ${JSON.stringify(description)},`,
        `        riskTier: ${JSON.stringify(riskTier)},`,
        '        inputShape: {',
        ...shapeFields,
        '        },',
        `        handler: async (${paramList}) => ${handlerBody},`,
        '    },',
    ].join('\n');
}

// Extracts the hand-written tool names from server/mcp/specialTools.ts, so
// the generator can refuse to emit a generated tool whose name collides with
// one of them. A collision would otherwise make `McpServer.registerTool`
// throw at runtime -- for ALL requests, since the MCP server is rebuilt
// fresh per request -- which surfaces to every Playground user as an opaque
// -32603 "Internal server error" with no way to tell which tool caused it
// from the client side.
function readSpecialToolNames() {
    const source = readFileSync(SPECIAL_TOOLS_TS_PATH, 'utf8');
    const names = new Set();
    for (const match of source.matchAll(/name:\s*'([^']+)'/g)) {
        names.add(match[1]);
    }
    return names;
}

function generateToolsRegion() {
    const schema = dumpOpenApiSchema();
    const components = schema.components || {};
    const specialToolNames = readSpecialToolNames();
    const generatedNames = new Set();
    const toolSources = [];
    const warnings = [];

    for (const [apiPath, methods] of Object.entries(schema.paths || {})) {
        for (const [method, operation] of Object.entries(methods)) {
            if (!(method in METHOD_TO_RISK_TIER)) continue; // skip trace/options/etc.
            const { summary, description, operationId } = operation;
            if (!operationId) {
                warnings.push(`SKIP ${method.toUpperCase()} ${apiPath}: no operationId set (add operation_id=... to the route decorator)`);
                continue;
            }
            // FastAPI auto-fills an operationId (and often summary/description from the
            // function name/docstring) when the route decorator doesn't set one explicitly.
            // Those auto-generated ids always end in "_<method>"; treat that as "not
            // explicitly exposed as an MCP tool" and skip it, per the same rule as a
            // missing operationId.
            if (new RegExp(`_${method}$`).test(operationId)) {
                warnings.push(`SKIP ${method.toUpperCase()} ${apiPath} (${operationId}): operationId looks auto-generated (add operation_id=... to the route decorator to opt in)`);
                continue;
            }
            if (!summary || !description) {
                warnings.push(`SKIP ${method.toUpperCase()} ${apiPath} (${operationId}): missing ${!summary ? 'summary' : ''}${!summary && !description ? ' and ' : ''}${!description ? 'description' : ''} on the route decorator`);
                continue;
            }
            if (specialToolNames.has(operationId)) {
                warnings.push(`SKIP ${method.toUpperCase()} ${apiPath} (${operationId}): operation_id collides with a hand-written special tool of the same name -- rename the operation_id`);
                continue;
            }
            if (generatedNames.has(operationId)) {
                warnings.push(`SKIP ${method.toUpperCase()} ${apiPath} (${operationId}): operation_id collides with another generated tool of the same name -- rename one of the operation_ids`);
                continue;
            }
            generatedNames.add(operationId);
            toolSources.push(buildToolSource(apiPath, method, operation, components));
        }
    }

    return { toolSources, warnings };
}

function main() {
    const checkOnly = process.argv.includes('--check');
    const { toolSources, warnings } = generateToolsRegion();

    for (const warning of warnings) {
        console.warn(`[generate-mcp-tools] ${warning}`);
    }
    console.log(`[generate-mcp-tools] generated ${toolSources.length} tool(s), skipped ${warnings.length}`);

    const updated = [
        '// Copyright 2026 Google LLC',
        '//',
        '// Licensed under the Apache License, Version 2.0 (the "License");',
        '// you may not use this file except in compliance with the License.',
        '// You may obtain a copy of the License at',
        '//',
        '//     https://www.apache.org/licenses/LICENSE-2.0',
        '//',
        '// Unless required by applicable law or agreed to in writing, software',
        '// distributed under the License is distributed on an "AS IS" BASIS,',
        '// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.',
        '// See the License for the specific language governing permissions and',
        '// limitations under the License.',
        '',
        '// -----------------------------------------------------------------------------',
        '// AUTO-GENERATED FILE -- produced by `node scripts/generate-mcp-tools.mjs` from',
        '// the llm_d_bench FastAPI app\'s OpenAPI schema (one tool per HTTP operation that',
        '// declares BOTH a summary and a description). DO NOT HAND-EDIT THIS FILE -- it is',
        '// overwritten wholesale every time the generator runs. To change a generated',
        '// tool, edit the route\'s FastAPI decorator (summary/description/operation_id)',
        '// instead and re-run the generator. riskTier is derived from HTTP method by',
        '// default (GET/HEAD -> read, POST/PUT/PATCH -> write, DELETE -> approve), with a',
        '// small named exception list (RISK_TIER_OVERRIDES in the generator script) for',
        '// operationIds known to have a real side effect despite their HTTP method.',
        '//',
        '// Hand-written tools that are not a 1:1 wrapper over one FastAPI operation live',
        '// in server/mcp/specialTools.ts instead -- edit that file directly, never this',
        '// one.',
        '// -----------------------------------------------------------------------------',
        '',
        "import { z } from 'zod';",
        "import { buildQuery, internalRequest } from './internal.ts';",
        "import { jsonRequest, toPlainResult, prismTools, findTool, type PrismTool } from './specialTools.ts';",
        '',
        'const generatedTools: PrismTool[] = [',
        toolSources.join('\n'),
        '];',
        '',
        '// Both halves of the catalog live in the SAME array (declared in',
        '// specialTools.ts) so hand-written tools like wait_for_status can look up any',
        '// tool, generated or hand-written, by name without a circular import.',
        'prismTools.push(...generatedTools);',
        '',
        'export { prismTools, findTool };',
        '',
    ].join('\n');

    const original = (() => {
        try {
            return readFileSync(TOOLS_TS_PATH, 'utf8');
        } catch {
            return null;
        }
    })();

    if (checkOnly) {
        if (updated !== original) {
            console.error('[generate-mcp-tools] tools.ts is out of date -- run `node scripts/generate-mcp-tools.mjs` and commit the result.');
            process.exit(1);
        }
        console.log('[generate-mcp-tools] up to date.');
        return;
    }

    writeFileSync(TOOLS_TS_PATH, updated);
    console.log(`[generate-mcp-tools] wrote ${TOOLS_TS_PATH}`);
}

main();
