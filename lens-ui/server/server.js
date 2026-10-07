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

import express from 'express';
import compression from 'compression';
import fs from 'fs';
import https from 'https';
import rateLimit from 'express-rate-limit';
import path from 'path';
import { fileURLToPath } from 'url';
import { authMiddleware } from './auth.ts';
import { backendApiProxyRouter } from './backend-api-proxy.ts';
import { deployRouter } from './deploy.ts';
import { remoteDeployRouter } from './remoteDeploy.ts';
import { candidateSearchRouter } from './candidateSearch.ts';
import { configurationRouter } from './configuration.ts';
import { guidePlanningRouter } from './guidePlanning.ts';
import { monitoringRouter } from './monitoring.ts';
import { mcpRouter } from './mcp/router.ts';
import { playgroundRouter } from './playground/chat.ts';
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const port = Number(process.env.SERVER_PORT) || Number(process.env.PORT) || 3000;
const host = process.env.HOST || process.env.SERVER_HOST || '0.0.0.0';

// Trust the first proxy (Cloud Run Load Balancer) to properly resolve X-Forwarded-For
app.set('trust proxy', 1);

// SSE progress must bypass gzip or small events remain buffered until completion.
app.use(compression({
    filter: (req, res) => !/^\/api\/agentic-deployments(?:\/[^/]+\/refine)?\/stream$/.test(req.path) && compression.filter(req, res),
}));
app.use(express.json({ limit: '50mb' }));
app.use(express.urlencoded({ limit: '50mb', extended: true }));

// Apply API rate limiting before feature routers.
const limiter = rateLimit({
    windowMs: 1 * 60 * 1000, // 1 minute
    max: 50000, // Effectively unlimited for local dev
    standardHeaders: true,
    legacyHeaders: false,
});

app.use('/api', limiter);
// Authenticate/authorize before any feature router; public paths and the auth
// entry points pass through, and proxied requests carry the session onward.
app.use(authMiddleware);
app.use(backendApiProxyRouter);
app.use(deployRouter);
app.use(remoteDeployRouter);
app.use(candidateSearchRouter);
app.use(configurationRouter);
app.use(guidePlanningRouter);
app.use(monitoringRouter);
app.use(mcpRouter);
app.use(playgroundRouter);

// --- API: Shared Configuration ---
app.get('/api/config', (req, res) => {
    const deploymentDefaults = {
        repository: process.env.LLM_D_ROOT || '',
        branch: process.env.LLM_D_BRANCH || 'tpc-llmdbench',
        httpProxy: process.env.HTTP_PROXY || '',
        httpsProxy: process.env.HTTPS_PROXY || '',
        noProxy: process.env.NO_PROXY || '',
        mountPath: process.env.LLM_D_MOUNT_PATH || '',
        mountModelName: process.env.LLM_D_MOUNT_MODEL_NAME || '',
    };

    res.json({
        deploymentDefaults,
    });
});

// Serve Static Assets (Production Build)
app.use(express.static(path.join(__dirname, '../dist'), { index: false }));

// Removed and unknown API endpoints must not return the SPA.
app.use('/api', (req, res) => res.status(404).json({ error: 'API endpoint not found' }));

// SPA Fallback: Serve index.html for any unknown routes
app.get('*', async (req, res) => {
    try {
        const fs = await import('fs/promises');
        const indexPath = path.join(__dirname, '../dist', 'index.html');

        const html = await fs.readFile(indexPath, 'utf-8');

        res.send(html);
    } catch (e) {
        console.error('Error serving index.html:', e);
        res.status(500).send('Internal Server Error');
    }
});

// Optional TLS: set TLS_CERT_FILE + TLS_KEY_FILE (and optionally TLS_CA_FILE
// for an intermediate chain) to terminate HTTPS directly in this process,
// e.g. for bare-metal installs where browsers require a secure context
// (HTTPS or localhost) for features like microphone access. Falls back to
// plain HTTP when unset, so this is fully backward compatible.
const tlsCertFile = process.env.TLS_CERT_FILE;
const tlsKeyFile = process.env.TLS_KEY_FILE;

if (tlsCertFile && tlsKeyFile) {
    const tlsOptions = {
        cert: fs.readFileSync(tlsCertFile),
        key: fs.readFileSync(tlsKeyFile),
    };
    if (process.env.TLS_CA_FILE) {
        tlsOptions.ca = fs.readFileSync(process.env.TLS_CA_FILE);
    }
    https.createServer(tlsOptions, app).listen(port, host, () => {
        console.log(`Server running on https://${host}:${port}`);
        console.log(`Mode: ${process.env.NODE_ENV || 'development'}`);
    });
} else {
    app.listen(port, host, () => {
        console.log(`Server running on http://${host}:${port}`);
        console.log(`Mode: ${process.env.NODE_ENV || 'development'}`);
    });
}
