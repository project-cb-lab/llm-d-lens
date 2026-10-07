// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import express from 'express';
import { fetchJsonWithTimeout } from './http';

export const configurationRouter = express.Router();

const CONFIGURATION_API_URL = (process.env.CONFIGURATION_API_URL || 'http://127.0.0.1:8090').replace(/\/$/, '');
const CONFIGURATION_TIMEOUT_MS = Number(process.env.CONFIGURATION_REQUEST_TIMEOUT_MS || 60_000);
const OPERATIONS = new Set(['resolve', 'render', 'save']);

configurationRouter.post('/api/configurations/:operation', async (req, res) => {
    const operation = String(req.params.operation || '');
    if (!OPERATIONS.has(operation)) return res.status(404).json({ error: 'Unknown configuration operation' });

    try {
        const { response, payload } = await fetchJsonWithTimeout(`${CONFIGURATION_API_URL}/api/v1/configurations/${operation}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body || {}),
        }, CONFIGURATION_TIMEOUT_MS);
        if (!response.ok) {
            const detail = payload.detail;
            const error = typeof detail === 'string' ? detail : `Configuration service returned HTTP ${response.status}`;
            return res.status(response.status).json({ ...payload, error });
        }
        return res.json(payload);
    } catch (error) {
        const message = error instanceof Error && error.name === 'AbortError'
            ? 'Configuration service timed out'
            : error instanceof Error ? error.message : 'Configuration service request failed';
        console.error('[Configuration API]', message);
        return res.status(error instanceof Error && error.name === 'AbortError' ? 504 : 502).json({ error: message });
    }
});
