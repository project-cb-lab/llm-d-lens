/** Local-time display; each domain can retain its invalid-input fallback. */
export function formatTimestamp(value, { invalid = String } = {}) {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? invalid(value) : date.toLocaleString();
}
