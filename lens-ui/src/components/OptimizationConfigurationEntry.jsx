import { useEffect } from 'react';
import { X } from 'lucide-react';
import OptimizationConfiguration from './OptimizationConfiguration';
import { readEvaluationIntent } from '../features/evaluation/transfer';
import { loadCluster } from './OptimizationWorkspace/clusterBackend';
import { startLocalDeployment } from './OptimizationWorkspace/remoteDeployBackend';

export default function OptimizationConfigurationEntry({ onNavigate }) {
    const intent = readEvaluationIntent();
    const returnTarget = intent?.return_target || 'optimization-evaluate';
    const requestedModelSource = intent?.runtime?.model_source;
    const deployOnly = intent?.operation === 'deploy-only';

    const deployPublishedConfigurations = async ({ artifacts }) => {
        const configurations = (artifacts || []).map((artifact) => artifact.deployable_configuration).filter(Boolean);
        if (!configurations.length) throw new Error('No deployable configurations were generated');
        const clusterId = sessionStorage.getItem('prism_cluster_server_id') || intent?.cluster_id;
        if (!clusterId) throw new Error('Select a cluster before deploying');
        const cluster = await loadCluster(clusterId);
        await startLocalDeployment(configurations, {
            cluster_session_id: sessionStorage.getItem('prism_cluster_session_id') || undefined,
            cluster_server_id: clusterId,
            deployment_source: {
                kind: 'model-market-advanced',
                resolved_repository: cluster.llmDRepoPath,
                ref: cluster.llmDRef || undefined,
            },
            deployment_name: intent?.deployment_name,
            description: intent?.deployment_description,
            model_market: { deployment_name: intent?.deployment_name },
        });
        onNavigate('optimization-deployments');
    };

    useEffect(() => {
        if (requestedModelSource) return undefined;
        const selectDirectLoading = () => {
            const select = [...document.querySelectorAll('.configuration-entry select')].find((element) => (
                [...element.options].some((option) => option.value === 'huggingface')
                && [...element.options].some((option) => option.value === 'auto-cache')
            ));
            if (!select || select.value === 'huggingface') return;
            const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
            setter?.call(select, 'huggingface');
            select.dispatchEvent(new Event('change', { bubbles: true }));
        };
        selectDirectLoading();
        const timers = [100, 500, 1200].map((delay) => window.setTimeout(selectDirectLoading, delay));
        return () => timers.forEach(window.clearTimeout);
    }, [requestedModelSource]);
    return <div className="configuration-entry relative">
        <button
            type="button"
            onClick={() => onNavigate(returnTarget)}
            aria-label="Close configuration"
            title="Close"
            className="fixed right-7 top-5 z-[70] inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 bg-slate-950/90 text-slate-400 shadow-lg backdrop-blur hover:border-slate-500 hover:bg-slate-800 hover:text-white"
        >
            <X className="h-4 w-4" />
        </button>
        <OptimizationConfiguration onNavigate={onNavigate} onPublished={deployOnly ? deployPublishedConfigurations : undefined} />
    </div>;
}
