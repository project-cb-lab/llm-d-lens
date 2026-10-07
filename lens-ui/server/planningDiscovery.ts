import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';
import { clusterSessionKubeconfig } from './clusterSession.ts';
import { fetchJsonWithTimeout } from './http.ts';
import { internalHeadersFor } from './internalAuth.ts';

const execute = promisify(execFile);
export type DiscoveryResult = Record<string, unknown>;
// eslint-disable-next-line no-unused-vars -- TypeScript function parameter names.
export type ClusterQuery = (kubeconfig: string | null, args: string[]) => Promise<DiscoveryResult | null>;

export const queryCluster: ClusterQuery = async (kubeconfig, args) => {
    try {
        // Only exchange the validated session identifier across the service
        // boundary. Default-context reads still belong to this Node host.
        const sessionId = kubeconfig ? path.basename(kubeconfig, '.yaml') : null;
        const resources: Record<string, string> = {
            'version -o json': 'version',
            'get nodes -o json': 'nodes',
            'get deviceclasses.resource.k8s.io -o json': 'deviceclasses',
            'get resourceslices.resource.k8s.io -o json': 'resourceslices',
            'get runtimeclasses.node.k8s.io -o json': 'runtimeclasses',
        };
        const resource = resources[args.join(' ')];
        if (sessionId && resource && clusterSessionKubeconfig(sessionId) === kubeconfig) {
            const base = process.env.SIMULATION_API_URL || process.env.OPTIMALBENCH_URL || 'http://127.0.0.1:8081';
            const route = `/api/cluster/sessions/${encodeURIComponent(sessionId)}/planning-discovery/${resource}`;
            const headers = internalHeadersFor('GET', route);
            const { response, payload } = await fetchJsonWithTimeout(new URL(route, base).href, { method: 'GET', headers }, 12_000);
            const result = payload?.result;
            return response.ok && result && typeof result === 'object' && !Array.isArray(result)
                ? result as DiscoveryResult : null;
        }
        const { stdout } = await execute('kubectl', [
            ...(kubeconfig ? ['--kubeconfig', kubeconfig] : []),
            '--request-timeout=10s', ...args,
        ], { timeout: 12_000, maxBuffer: 16 * 1024 * 1024, encoding: 'utf8' });
        return JSON.parse(stdout) as DiscoveryResult;
    } catch { return null; }
};

// All four reads are independent. A slow optional DRA query must not serialize
// the other reads or block unrelated Express requests on the Node event loop.
export async function inspectPlanningCluster(kubeconfig: string | null, query: ClusterQuery = queryCluster) {
    const [version, nodes, deviceClasses, resourceSlices, runtimeClasses] = await Promise.all([
        query(kubeconfig, ['version', '-o', 'json']),
        query(kubeconfig, ['get', 'nodes', '-o', 'json']),
        query(kubeconfig, ['get', 'deviceclasses.resource.k8s.io', '-o', 'json']),
        query(kubeconfig, ['get', 'resourceslices.resource.k8s.io', '-o', 'json']),
        query(kubeconfig, ['get', 'runtimeclasses.node.k8s.io', '-o', 'json']),
    ]);
    return version && nodes ? { version, nodes, deviceClasses, resourceSlices, runtimeClasses } : null;
}
