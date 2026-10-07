import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execute = promisify(execFile);
type Headers = Record<string, string>;

async function curlText(url: string, headers: Headers): Promise<string> {
    const args = ['-fsSL', '--max-redirs', '0', '--connect-timeout', '10', '--max-time', '30'];
    for (const [name, value] of Object.entries(headers)) args.push('-H', `${name}: ${value}`);
    args.push(url);
    try {
        return (await execute('curl', args, { encoding: 'utf8', timeout: 35_000, maxBuffer: 16 * 1024 * 1024 })).stdout;
    } catch {
        // Child-process errors contain command arguments, potentially including tokens.
        throw new Error('Guide source download failed');
    }
}

export async function readGuideSource(url: string, headers: Headers, {
    environment = process.env,
    direct = globalThis.fetch,
    proxyAware = curlText,
}: {
    environment?: Record<string, string | undefined>;
    direct?: typeof fetch;
    // eslint-disable-next-line no-unused-vars -- TypeScript function parameter names.
    proxyAware?: (url: string, headers: Headers) => Promise<string>;
} = {}): Promise<string> {
    const source = new URL(url);
    if (source.protocol !== 'https:' || source.hostname !== 'api.github.com' || source.port
        || source.username || source.password) {
        throw new Error('Guide source must use the official GitHub API host');
    }
    const path = source.pathname.split('/').map((segment) => encodeURIComponent(decodeURIComponent(segment))).join('/');
    const query = [...source.searchParams]
        .map(([name, value]) => `${encodeURIComponent(name)}=${encodeURIComponent(value)}`)
        .join('&');
    const safeUrl = `https://api.github.com${path}${query ? `?${query}` : ''}`;
    // Node 22's native fetch does not automatically honor these variables.
    // curl honors the configured proxy and NO_PROXY without a failed direct attempt.
    if (environment.HTTPS_PROXY || environment.https_proxy || environment.ALL_PROXY || environment.all_proxy) {
        return proxyAware(safeUrl, headers);
    }
    try {
        const response = await direct(safeUrl, { headers, redirect: 'manual', signal: AbortSignal.timeout(15_000) });
        if (!response.ok) throw Object.assign(new Error(`Official llm-d guide request failed (${response.status})`), { status: response.status });
        return await response.text();
    } catch (error) {
        try { return await proxyAware(safeUrl, headers); } catch { throw error; }
    }
}
