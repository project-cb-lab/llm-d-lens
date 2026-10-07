# The Lens MCP tool catalog (server/mcp/tools.ts) is a fully generated build
# artifact -- see scripts/generate-mcp-tools.mjs and server/mcp/specialTools.ts
# (the real hand-written source). It is gitignored, so it must be produced
# here rather than assumed to already exist in the build context. Generating
# it needs BOTH Python (to dump the llm_d_bench FastAPI app's OpenAPI schema)
# and Node (to run the generator script itself), so this starts from the
# Python image and layers Node on top via apt -- the reverse (starting from
# a Node image and installing Python) hit a Python 3.13/numpy wheel
# incompatibility for the backend's aiconfigurator dependency.
FROM python:3.14-slim as mcp-tools

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY llm_d_bench ./llm_d_bench
RUN python3 -m venv .venv \
    && .venv/bin/pip install --no-cache-dir --upgrade pip \
    && .venv/bin/pip install --no-cache-dir -e .

COPY scripts/generate-mcp-tools.mjs ./scripts/generate-mcp-tools.mjs
COPY server/mcp/specialTools.ts ./server/mcp/specialTools.ts
RUN node scripts/generate-mcp-tools.mjs

# Build stage
FROM node:26-alpine as build

WORKDIR /app

COPY package*.json ./
RUN npm install

COPY . .
COPY --from=mcp-tools /app/server/mcp/tools.ts ./server/mcp/tools.ts
RUN npm run build

# Serve stage
FROM node:26-alpine

WORKDIR /app

# Guide planning renders official llm-d Kustomize overlays and inspects an
# available Kubernetes context. Git is required by remote Kustomize URLs.
RUN apk add --no-cache curl git kubectl

# Install production dependencies for server
COPY package*.json ./
RUN npm install --omit=dev

# Copy server and built assets
COPY server ./server
COPY --from=mcp-tools /app/server/mcp/tools.ts ./server/mcp/tools.ts
COPY src/utils ./src/utils
COPY --from=build /app/dist ./dist

ENV LENS_DATA_DIR=/var/lib/lens
ENV LENS_CACHE_DIR=/var/cache/lens
ENV LENS_LOG_DIR=/var/log/lens
VOLUME ["/var/lib/lens", "/var/cache/lens", "/var/log/lens"]
ENV PORT=8080
EXPOSE 8080

CMD ["npm", "start"]
