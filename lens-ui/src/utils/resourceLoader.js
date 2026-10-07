// Coalesce metadata requests from the wizard and surrounding workspace.
// Failed loads remain retryable; callers can explicitly refresh the resource.
export function createResourceLoader(load, ttlMs = 60_000) {
    let cached = null;
    let expiresAt = 0;
    return ({ refresh = false } = {}) => {
        if (!refresh && cached && Date.now() < expiresAt) return cached;
        expiresAt = Number.POSITIVE_INFINITY;
        const pending = Promise.resolve().then(load).then((value) => {
            if (cached === pending) expiresAt = Date.now() + ttlMs;
            return value;
        }).catch((error) => {
            if (cached === pending) cached = null;
            throw error;
        });
        cached = pending;
        return pending;
    };
}

/** Per-consumer latest-request lifecycle. Separate from metadata TTL caching. */
export function createLatestResourceRequest() {
    let current = null;
    return {
        get pending() { return current !== null; },
        cancel() {
            const previous = current;
            current = null;
            previous?.abort();
        },
        async run(load, { onSuccess, onError, onSettled } = {}, { skipIfPending = false } = {}) {
            if (skipIfPending && current) return;
            current?.abort();
            const controller = new AbortController();
            current = controller;
            try {
                const value = await load(controller.signal);
                if (current === controller) onSuccess?.(value);
            } catch (error) {
                if (current === controller && error?.name !== 'AbortError') onError?.(error);
            } finally {
                if (current === controller) {
                    current = null;
                    onSettled?.();
                }
            }
        },
    };
}
