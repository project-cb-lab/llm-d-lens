/** Shared client for Server-Sent-Events endpoints (POST + streaming fetch body).
 * Native EventSource only supports GET without custom headers, but our SSE
 * endpoints take a JSON body and need the CSRF header, so we parse the
 * `event:`/`data:` frames out of a streamed fetch response instead. */
import { problemError, readJson } from './httpClient.js';

function csrfToken() {
    if (typeof document === 'undefined') return '';
    const match = document.cookie.match(/(?:^|; )prism_csrf=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : '';
}

/**
 * POST `payload` to `path` and invoke `onEvent(type, data)` for every SSE
 * frame the server streams back, until the response body ends.
 */
export async function streamSseRequest(path, payload, onEvent, signal) {
    const headers = { 'Content-Type': 'application/json', Accept: 'text/event-stream' };
    const csrf = csrfToken();
    if (csrf) headers['X-Prism-CSRF'] = csrf;
    const response = await fetch(path, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
        signal,
    });
    if (!response.ok || !response.body) {
        const body = await readJson(response, {});
        throw problemError(body, response);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const chunks = buffer.split('\n\n');
        buffer = chunks.pop() || '';
        for (const chunk of chunks) {
            const lines = chunk.split('\n');
            const eventLine = lines.find((line) => line.startsWith('event:'));
            const dataLine = lines.find((line) => line.startsWith('data:'));
            if (!eventLine || !dataLine) continue;
            const type = eventLine.slice(6).trim();
            const data = JSON.parse(dataLine.slice(5).trim());
            onEvent(type, data);
        }
        if (done) break;
    }
}
