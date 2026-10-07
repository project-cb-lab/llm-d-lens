export function sameOriginUrl(path: string, baseUrl: string): URL {
    const base = new URL(baseUrl);
    if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password) {
        throw new Error('Configured upstream must be an HTTP(S) URL without credentials');
    }
    const candidate = new URL(path, base);
    if (candidate.origin !== base.origin) throw new Error('Upstream request must remain on the configured origin');
    const resolved = new URL(base.origin);
    resolved.pathname = candidate.pathname.split('/').map((segment) => encodeURIComponent(decodeURIComponent(segment))).join('/');
    resolved.search = [...candidate.searchParams]
        .map(([name, value]) => `${encodeURIComponent(name)}=${encodeURIComponent(value)}`)
        .join('&');
    return resolved;
}