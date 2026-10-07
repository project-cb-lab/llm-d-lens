// Carries a cluster selection made outside the Model Market page (e.g. the
// "Plan & Deploy" button on the Clusters page) across the view navigation so
// the One-click deployment form can pre-select it, mirroring the pattern
// used by `src/features/evaluation/transfer.js`.
const PENDING_CLUSTER_KEY = 'prism_model_market_pending_cluster';

export function storePendingModelMarketCluster(clusterId, storage = sessionStorage) {
    if (!clusterId) {
        storage.removeItem(PENDING_CLUSTER_KEY);
        return;
    }
    storage.setItem(PENDING_CLUSTER_KEY, clusterId);
}

export function consumePendingModelMarketCluster(storage = sessionStorage) {
    const clusterId = storage.getItem(PENDING_CLUSTER_KEY);
    storage.removeItem(PENDING_CLUSTER_KEY);
    return clusterId || null;
}
