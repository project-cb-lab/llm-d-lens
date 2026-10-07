/** JSON upstream transport; callers own HTTP status and error mapping.
 * The timeout covers both fetching headers and consuming the response body.
 */
export async function fetchJsonWithTimeout(
    url: string,
    init: RequestInit,
    timeoutMs: number,
): Promise<{ response: Response; payload: Record<string, unknown> }> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const signal = init.signal ? AbortSignal.any([init.signal, controller.signal]) : controller.signal;
    try {
        const response = await fetch(url, { ...init, signal });
        const text = await response.text();
        let payload: Record<string, unknown>;
        try {
            payload = text ? JSON.parse(text) : {};
        } catch {
            payload = { detail: text };
        }
        return { response, payload };
    } finally {
        clearTimeout(timeout);
    }
}
