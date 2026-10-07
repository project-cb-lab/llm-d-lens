import { loadClusters } from '../OptimizationWorkspace/clusterBackend';

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

// Reads Server-Sent Events emitted by POST /api/playground/chat
// (server/playground/chat.ts). fetch() is used instead of EventSource
// because EventSource cannot send a POST body.
// `pendingResume` (instead of `messages`) resumes a turn paused on a
// `confirm_required` event, once the user has approved or rejected the
// write/approve-tier tool call that paused it: { conversation, toolCallId,
// decision }, where `conversation` is exactly the resume blob that event
// carried. `readOnly` restricts the turn to read-tier Lens MCP tools only
// (used for voice-transcribed messages, see PlaygroundPage.jsx).
export async function streamPlaygroundChat({ deployment, messages, pendingResume, readOnly }, onEvent, signal) {
    const response = await fetch('/api/playground/chat', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(pendingResume ? { deployment, pendingResume } : { deployment, messages, readOnly }),
        signal,
    });
    if (!response.ok || !response.body) {
        const detail = await response.text().catch(() => '');
        throw new Error(`Playground chat request failed (${response.status}): ${detail.slice(0, 300)}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    // Minimal SSE parser: events are separated by a blank line and carry an
    // "event:" line followed by a "data:" line (see sseWrite in chat.ts).
    for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split('\n\n');
        buffer = chunks.pop() ?? '';
        for (const chunk of chunks) {
            const lines = chunk.split('\n');
            const eventLine = lines.find((line) => line.startsWith('event:'));
            const dataLine = lines.find((line) => line.startsWith('data:'));
            if (!eventLine || !dataLine) continue;
            const type = eventLine.slice('event:'.length).trim();
            let data = {};
            try {
                data = JSON.parse(dataLine.slice('data:'.length).trim());
            } catch {
                data = {};
            }
            onEvent(type, data);
        }
    }
}

// Clusters and cluster sessions are Prism-wide concepts (see
// llm_d_bench/cluster/router.py); reused as-is here so "pick a cluster, then
// pick a deployment on it" matches every other Prism workflow (e.g. Model
// Market) instead of inventing a parallel concept just for the Playground.
export async function listClusters() {
    const payload = await loadClusters();
    return payload.items || [];
}

export { openClusterSession } from '../OptimizationWorkspace/clusterBackend';
