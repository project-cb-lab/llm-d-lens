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

// Resolves a connected cluster session to its private kubeconfig on disk.
// The FastAPI cluster service owns the session lifecycle and writes one file
// per session; file existence is the shared "active" signal used by Express.

import fs from 'node:fs';
import { storagePath } from './storagePaths.ts';
import path from 'node:path';

const SESSION_KUBECONFIG_DIR = storagePath('data', ['credentials', 'clusters', 'cluster_sessions']);

const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function clusterSessionKubeconfig(sessionId: unknown): string | null {
    const id = String(sessionId || '');
    if (!SESSION_ID.test(id)) return null;
    const kubeconfig = path.join(SESSION_KUBECONFIG_DIR, `${id}.yaml`);
    return fs.existsSync(kubeconfig) ? kubeconfig : null;
}
