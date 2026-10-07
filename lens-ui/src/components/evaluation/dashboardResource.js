// Keep the last successful result across navigation, while always refreshing on
// entry. Slow polling requests are shared instead of accumulating every 5s.
export function createDashboardResource(load) {
    let snapshot = null;
    let pending = null;
    return {
        getSnapshot: () => snapshot,
        refresh() {
            if (!pending) {
                pending = Promise.resolve().then(load).then(value => {
                    snapshot = value;
                    return value;
                }).finally(() => { pending = null; });
            }
            return pending;
        },
    };
}
