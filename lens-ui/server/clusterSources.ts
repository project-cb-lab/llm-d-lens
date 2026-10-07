// Source locations are owned by the clusters API, never inferred from local caches.
import { internalHeadersFor } from './internalAuth.ts';

type ClusterSelection = { clusterId?: unknown; clusterSessionId?: unknown };
type ClusterSources = { id: string; llmDRepoPath?: string; llmDRef?: string; llmDBenchmarkRepoPath?: string };

export async function loadClusterSources(selection: ClusterSelection): Promise<ClusterSources> {
    const base = process.env.SIMULATION_API_URL || process.env.OPTIMALBENCH_URL || 'http://127.0.0.1:8081';
    const read = async (route: string) => {
        const headers = new Headers({ accept: 'application/json', ...internalHeadersFor('GET', route) });
        const response = await fetch(new URL(route, base), { headers, signal: AbortSignal.timeout(15_000) });
        const payload = await response.json();
        if (!response.ok) throw Object.assign(new Error(payload.detail || payload.error || 'Could not load cluster sources'), { status: response.status });
        return payload;
    };
    let clusterId = String(selection.clusterId || '').trim();
    if (selection.clusterSessionId) {
        const session = await read(`/api/cluster-overview/sessions/${encodeURIComponent(String(selection.clusterSessionId))}`);
        if (clusterId && clusterId !== session.serverId) throw Object.assign(new Error('Cluster and session do not match'), { status: 409 });
        clusterId = session.serverId;
    }
    if (!clusterId) throw Object.assign(new Error('Select a cluster and download its Software Versions first'), { status: 400 });
    const payload = await read('/api/cluster/clusters');
    const cluster = payload.items?.find((item: ClusterSources) => item.id === clusterId);
    if (!cluster) throw Object.assign(new Error('Selected cluster was not found'), { status: 404 });
    if (!cluster.llmDRepoPath || cluster.deploymentSource?.resolved_from === 'unavailable') {
        throw Object.assign(new Error('Download the selected cluster\'s llm-d Software Versions first'), { status: 409 });
    }
    return cluster;
}
