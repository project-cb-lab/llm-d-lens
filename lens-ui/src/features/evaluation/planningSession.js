// Reuse only recent, identical previews within one editor. Cluster state is
// checked again when this short window expires, and by deployment validation.
export function createPlanningSession(plan, { now = Date.now, ttlMs = 60_000 } = {}) {
    const entries = new Map();
    return async (request) => {
        // Custom/local sources can change without their request parameters changing.
        if (request.source?.mode !== 'official') return plan(request);
        const key = JSON.stringify(request);
        const cached = entries.get(key);
        if (cached && cached.expiresAt > now()) return cached.promise;
        const entry = { expiresAt: now() + ttlMs, promise: null };
        entry.promise = Promise.resolve().then(() => plan(request)).then((result) => {
            if (result.validation?.status === 'invalid' || result.validation?.errors?.length) {
                if (entries.get(key) === entry) entries.delete(key);
            }
            else entry.expiresAt = now() + ttlMs;
            return result;
        }).catch((error) => {
            if (entries.get(key) === entry) entries.delete(key);
            throw error;
        });
        entries.set(key, entry);
        if (entries.size > 16) entries.delete(entries.keys().next().value);
        return entry.promise;
    };
}
