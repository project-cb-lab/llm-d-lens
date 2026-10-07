/** JSON transport shared by domain clients; streaming/downloads use fetch directly. */
export function problemError(payload, response) {
    const detail = payload?.detail;
    const structured = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail : {};
    const base = structured.message || structured.code || payload?.error
        || (typeof detail === 'string' ? detail : null) || payload?.message || payload?.title
        || `Request failed (${response.status})`;
    // Surface the server's request id so the full traceback can be found in logs.
    const message = payload?.requestId ? `${base} (ref ${payload.requestId})` : base;
    const error = new Error(message);
    error.status = response.status;
    error.code = payload?.code || structured.code || '';
    error.title = payload?.title || '';
    error.details = detail ?? payload?.error ?? null;
    error.retryable = Boolean(structured.retryable ?? payload?.retryable);
    error.body = payload;
    return error;
}

export async function readJson(response, fallback = {}) {
    if (response.status === 204) return fallback;
    return response.json().catch(() => fallback);
}

function readCookie(name) {
    if (typeof document === 'undefined') return '';
    const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
    return match ? decodeURIComponent(match[1]) : '';
}

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

export async function requestJson(url, options = {}, { fallback = {}, errorFactory = problemError, strictJson = false, expectedStatus } = {}) {
    const headers = new Headers(options.headers);
    if (!headers.has('Accept')) headers.set('Accept', 'application/json');
    const method = (options.method || 'GET').toUpperCase();
    if (!SAFE_METHODS.has(method) && !headers.has('X-Prism-CSRF')) {
        const csrf = readCookie('prism_csrf');
        if (csrf) headers.set('X-Prism-CSRF', csrf);
    }
    const response = await fetch(url, { credentials: 'include', ...options, headers });
    const payload = strictJson && response.ok ? await response.json() : await readJson(response, fallback);
    if (!response.ok || (expectedStatus !== undefined && response.status !== expectedStatus)) {
        if (response.status === 401 && typeof window !== 'undefined') {
            window.dispatchEvent(new CustomEvent('prism:auth-expired'));
        }
        throw errorFactory(payload, response);
    }
    return payload;
}

export function postJson(url, body, options = {}) {
    const headers = new Headers(options.headers);
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    return requestJson(url, { ...options, method: 'POST', headers, body: JSON.stringify(body) });
}
