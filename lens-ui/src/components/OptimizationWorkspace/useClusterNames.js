import { useMemo } from 'react';
import { useResourceList } from '../../hooks/useResourceList';
import { loadClusters } from './clusterBackend';

async function loadClusterRows({ signal }) {
    const payload = await loadClusters({ signal });
    return Array.isArray(payload?.items) ? payload.items : [];
}

/** Optional display metadata; failed lookups leave raw-ID fallbacks to the caller. */
export function useClusterNames() {
    const { items: clusters, load: refresh } = useResourceList(loadClusterRows);
    const clusterNameById = useMemo(() => Object.fromEntries(
        clusters.filter(cluster => cluster?.id).map(cluster => [cluster.id, cluster.name || cluster.id])
    ), [clusters]);
    return { clusters, clusterNameById, refresh };
}
