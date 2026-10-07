import { useClipboard } from '../../hooks/useClipboard';
import { createElement } from 'react';
import {
    AlertTriangle,
    Check,
    Clock,
    Copy,
    Cpu,
    FileText,
    Globe,
    HardDrive,
    Info,
    Layers,
    Server,
    Sliders,
} from 'lucide-react';
import { Modal } from '../ui/Modal';
import {
    deploymentEndpoint,
    deploymentKind,
    deploymentTitle,
    formatTimestamp,
    statusGroup,
    statusLabel,
    statusTone,
} from './deploymentPresentation';

function CopyIconButton({ value, label = 'Copy' }) {
    const { copied, copy } = useClipboard();
    const handleCopy = () => copy(value);

    return (
        <button
            type="button"
            onClick={handleCopy}
            className="inline-flex shrink-0 items-center rounded-md border border-slate-700/80 p-1 text-slate-400 transition hover:border-emerald-500/40 hover:text-slate-100"
            title={`${label}: ${value}`}
        >
            {copied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
        </button>
    );
}

function Fact({ label, value, mono, copyable }) {
    const displayValue = value === null || value === undefined || value === '' ? '—' : String(value);

    return (
        <div className="min-w-0">
            <dt className="text-[10px] font-bold uppercase tracking-wider text-slate-500">{label}</dt>
            <dd className={`mt-0.5 flex items-center gap-1.5 break-all text-xs text-slate-200 ${mono ? 'font-mono text-[11px]' : ''}`}>
                <span className="min-w-0 truncate" title={displayValue}>{displayValue}</span>
                {copyable && value && <CopyIconButton value={String(value)} label={label} />}
            </dd>
        </div>
    );
}

function SectionCard({ icon, title, children }) {
    return (
        <div className="rounded-xl border border-slate-800/80 bg-slate-900/50 p-3.5 backdrop-blur-sm">
            <div className="mb-3 flex items-center gap-2 border-b border-slate-800/60 pb-2 text-xs font-semibold text-slate-300">
                {createElement(icon, { size: 14, className: 'text-cyan-400' })}
                <span>{title}</span>
            </div>
            {children}
        </div>
    );
}

export function DeploymentDetailsModal({ deployment, clusterNameById = {}, onClose, onViewPodsLogs }) {
    if (!deployment) return null;

    const isFailed = deployment.status === 'failed' || Boolean(deployment.failure);
    const failure = deployment.failure;
    const customParams = Array.isArray(deployment.custom_parameters) ? deployment.custom_parameters : [];

    const storageDetail = deployment.storage_volume_id
        ? `Volume: ${deployment.storage_volume_id}`
        : deployment.pvc_name
        ? `PVC: ${deployment.pvc_name}`
        : deployment.mount_path
        ? `Path: ${deployment.mount_path}`
        : '—';

    const prefixCachingLabel = deployment.enable_prefix_caching !== undefined && deployment.enable_prefix_caching !== null
        ? (String(deployment.enable_prefix_caching) === 'true' ? 'Enabled' : 'Disabled')
        : '—';

    return (
        <Modal isOpen onClose={onClose} title={deploymentTitle(deployment)} subtitle="Deployment execution details" variant="drawer" size="xl">
            <div className="flex flex-col gap-4 text-xs">
                {/* Header status bar */}
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800/80 pb-3">
                    <div className="flex items-center gap-2">
                        <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ${statusTone(deployment.status)}`}>
                            {statusLabel(deployment.status)}
                        </span>
                        {deployment.pending && <span className="text-[11px] text-slate-500">Execution pending</span>}
                    </div>
                    {onViewPodsLogs && (
                        <button
                            type="button"
                            onClick={() => {
                                onClose();
                                onViewPodsLogs(deployment);
                            }}
                            className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium transition ${
                                statusGroup(deployment.status) === 'In progress'
                                    ? 'border-sky-500/40 bg-sky-500/10 text-sky-300 animate-pulse hover:bg-sky-500/20'
                                    : 'border-slate-700 bg-slate-800/80 text-emerald-300 hover:border-emerald-500/50 hover:bg-emerald-500/10'
                            }`}
                        >
                            <FileText size={14} />
                            View Pods & Logs
                        </button>
                    )}
                </div>

                {/* Failure Banner */}
                {isFailed && (
                    <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-rose-200">
                        <div className="flex items-center gap-2 font-semibold text-rose-300">
                            <AlertTriangle size={15} className="shrink-0" />
                            <span>{failure?.title || 'Deployment Execution Error'}</span>
                        </div>
                        <p className="mt-1 whitespace-pre-wrap text-[11px] leading-relaxed text-rose-200/90">
                            {failure?.detail || deployment.description || 'An error occurred during deployment execution.'}
                        </p>
                        {failure?.code && (
                            <div className="mt-2 font-mono text-[10px] text-rose-300/80">Code: {failure.code}</div>
                        )}
                    </div>
                )}

                {/* Section 1: Basic Info & Strategy */}
                <SectionCard icon={Info} title="Basic Information">
                    <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <Fact label="Display Name" value={deployment.display_name || deploymentTitle(deployment)} />
                        <Fact label="Internal Name" value={deployment.name} />
                        <Fact label="Type / Strategy" value={deploymentKind(deployment)} />
                        <Fact label="Provider Reference" value={deployment.guide} mono />
                        <Fact label="Preserve Deployment" value={deployment.preserve_deployment ? 'Yes' : 'No'} />
                        <Fact label="Status" value={statusLabel(deployment.status)} />
                    </dl>
                    <div className="mt-3 border-t border-slate-800/40 pt-2">
                        <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Description</div>
                        <p className="mt-0.5 whitespace-pre-wrap leading-relaxed text-slate-300">
                            {deployment.description || 'No description'}
                        </p>
                    </div>
                </SectionCard>

                {/* Section 2: Model & Engine Topology */}
                <SectionCard icon={Cpu} title="Model & Serving Engine">
                    <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3">
                        <Fact label="Model Name" value={deployment.model} />
                        <Fact label="Backend Engine" value={deployment.backend || 'vLLM'} />
                        <Fact label="Replicas" value={deployment.replicas} />
                        <Fact label="Tensor Parallel Size" value={deployment.tensor_parallel_size} />
                        <Fact label="Max Model Length" value={deployment.max_model_len} />
                        <Fact label="GPU Memory Utilization" value={deployment.gpu_memory_utilization} />
                        <Fact label="Prefix Caching" value={prefixCachingLabel} />
                        <Fact label="Max Sequences" value={deployment.max_num_seqs} />
                        <Fact label="Max Batched Tokens" value={deployment.max_num_batched_tokens} />
                    </dl>
                </SectionCard>

                {/* Section 3: Cluster & Environment */}
                <SectionCard icon={Server} title="Cluster & Environment">
                    <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <Fact label="Cluster" value={clusterNameById[deployment.cluster_id] || deployment.cluster_id} />
                        <Fact label="Namespace" value={deployment.namespace} mono copyable />
                        <Fact label="Cluster Session ID" value={deployment.cluster_session_id} mono copyable />
                        <Fact label="Storage Type" value={deployment.storage_type} />
                        <Fact label="Storage Reference" value={storageDetail} mono />
                        <Fact label="Container Image" value={deployment.image} mono copyable />
                    </dl>
                </SectionCard>

                {/* Section 4: Network & Endpoints */}
                <SectionCard icon={Globe} title="Network & Endpoints">
                    <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <Fact label="Internal Endpoint" value={deploymentEndpoint(deployment)} mono copyable />
                        <Fact label="Forwarded Endpoint" value={deployment.forwarded_endpoint} mono copyable />
                        {deployment.baseline_endpoint && (
                            <Fact label="Baseline Endpoint" value={deployment.baseline_endpoint} mono copyable />
                        )}
                        {deployment.service_ref && (
                            <Fact label="Service Reference" value={deployment.service_ref} mono />
                        )}
                    </dl>
                </SectionCard>

                {/* Section 5: Custom Parameters */}
                {customParams.length > 0 && (
                    <SectionCard icon={Sliders} title="Custom Parameters">
                        <div className="overflow-x-auto">
                            <table className="w-full text-left text-[11px]">
                                <thead>
                                    <tr className="border-b border-slate-800 text-slate-500">
                                        <th className="py-1.5 pr-2">Target</th>
                                        <th className="py-1.5 px-2">Kind</th>
                                        <th className="py-1.5 px-2">Name</th>
                                        <th className="py-1.5 pl-2">Value</th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-slate-800/50">
                                    {customParams.map((param, index) => (
                                        <tr key={index} className="text-slate-300 font-mono">
                                            <td className="py-1 pr-2 text-slate-400">{param.target || '—'}</td>
                                            <td className="py-1 px-2 text-slate-400">{param.kind || '—'}</td>
                                            <td className="py-1 px-2 text-cyan-300">{param.name || '—'}</td>
                                            <td className="py-1 pl-2 text-slate-200">{String(param.value ?? '—')}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </SectionCard>
                )}

                {/* Section 6: Audit & Timeline */}
                <SectionCard icon={Clock} title="Audit & Timeline">
                    <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <Fact label="Execution ID" value={deployment.execution_id} mono copyable />
                        <Fact label="Created At" value={formatTimestamp(deployment.created_at)} />
                        <Fact label="Updated At" value={formatTimestamp(deployment.updated_at)} />
                    </dl>
                </SectionCard>

                <p className="rounded-lg border border-slate-800/60 bg-slate-900/40 px-3 py-2 text-[11px] text-slate-500">
                    {deployment.pending
                        ? 'Prism is rendering the deployment and creating its execution record. This row refreshes automatically once the Kubernetes execution is available.'
                        : 'Runtime configuration is immutable. Model, namespace, image, topology, endpoint, and cluster cannot be edited here.'}
                </p>
            </div>
        </Modal>
    );
}

export default DeploymentDetailsModal;
