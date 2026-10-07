---
description: "Require accurate API descriptions and contract documentation for every new or modified API endpoint."
applyTo: "**/*.{py,js,jsx,ts,tsx}"
---

# API Description Requirements

Apply these requirements whenever a change adds, modifies, deprecates, or removes an externally callable API, including FastAPI routes, HTTP handlers, RPC/MCP tools, and their request or response contracts.

1. Add or update the API's description at its public definition. For FastAPI, use the route decorator's `summary` and `description` where supported. For other API surfaces, use the established local documentation mechanism.
2. Describe the actual behavior, not the implementation intent. State the operation, important inputs, successful result, side effects, and meaningful constraints or failure conditions when they are part of the contract.
3. Keep descriptions consistent with the implementation, validation, authorization, defaults, response status codes, and request/response schemas in the same change.
4. Update request, response, field, and enum descriptions when a contract change makes existing descriptions incomplete or inaccurate.
5. When an API is deprecated or removed, update its description and any maintained API reference or consumer-facing documentation to communicate the migration path or removal.
6. Update the matching Fern doc page under `docs/fern/pages/api-reference/<module>.mdx` in the same change: keep the endpoint's REST API tab (method, path, request/response fields, JSON example) and Python API tab (the real callable and a working sample) accurate — see [AGENTS.md § Keep the docs site in sync](../../AGENTS.md#keep-the-docs-site-in-sync). This applies to any add, modify, deprecate, or remove, not only removals.

Before completing API work, verify that the public description matches the implemented behavior, that generated OpenAPI or equivalent API metadata exposes the updated contract where applicable, and that `npm run docs:check` reports no new errors.