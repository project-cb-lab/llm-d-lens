import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Activity, BarChart3, ChevronDown, GitBranch, Loader2, Pause, Play, Rocket, Trash2, Webcam } from 'lucide-react';
import {
    getClusterStackLinks,
    getDeploymentMonitoring,
    manageDeploymentMonitoring,
} from '../ClusterMonitoringStack/clusterMonitoringStackBackend';

const CENTRAL_NAMESPACE = 'llm-d-monitoring';
const GRAFANA_PERFORMANCE_DASHBOARD = '/d/llm-d-performance/llm-d-performance-dashboard';

const TONE_CLASSES = {
    enabled: 'text-slate-400 hover:bg-slate-800/70 hover:text-slate-100',
    disabled: 'text-slate-400 hover:bg-slate-800/70 hover:text-slate-100',
    absent: 'text-slate-400 hover:bg-slate-800/70 hover:text-slate-100',
    unreachable: 'text-slate-400 hover:bg-slate-800/70 hover:text-slate-100',
    neutral: 'text-slate-400 hover:bg-slate-800/70 hover:text-slate-100',
};

function MenuItem({ onClick, disabled, icon, label, danger = false }) {
    return (
        <button
            type="button"
            role="menuitem"
            onClick={onClick}
            disabled={disabled}
            className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                danger ? 'text-rose-400 hover:bg-rose-500/10' : 'text-slate-200 hover:bg-slate-800'
            }`}
        >
            {icon}
            {label}
        </button>
    );
}

export function DeploymentMonitoringButton({ deployment, onNavigate }) {
    const [status, setStatus] = useState(null);
    const [loadingStatus, setLoadingStatus] = useState(false);
    const [open, setOpen] = useState(false);
    const [menuPosition, setMenuPosition] = useState(null);
    const [actionBusy, setActionBusy] = useState(false);
    const [openingLink, setOpeningLink] = useState('');
    const [error, setError] = useState('');
    const buttonRef = useRef(null);

    const clusterId = deployment.cluster_id;
    const executionId = deployment.execution_id;

    useEffect(() => {
        if (!open || !clusterId) return undefined;
        let cancelled = false;
        setLoadingStatus(true);
        setError('');
        getDeploymentMonitoring(executionId, { clusterId })
            .then((payload) => { if (!cancelled) setStatus(payload?.status || 'neutral'); })
            .catch((failure) => { if (!cancelled) setError(failure.message || 'Unable to load monitoring status'); })
            .finally(() => { if (!cancelled) setLoadingStatus(false); });
        return () => { cancelled = true; };
    }, [open, clusterId, executionId]);

    const toggleOpen = () => {
        if (!open) {
            const rect = buttonRef.current?.getBoundingClientRect();
            if (rect) setMenuPosition({ top: rect.bottom, right: window.innerWidth - rect.right });
        }
        setOpen((current) => !current);
    };

    const runAction = async (action) => {
        setActionBusy(true);
        setError('');
        try {
            await manageDeploymentMonitoring(executionId, action, { clusterId });
            const payload = await getDeploymentMonitoring(executionId, { clusterId });
            setStatus(payload?.status || 'neutral');
        } catch (failure) {
            setError(failure.message || `Unable to ${action} monitoring`);
        } finally {
            setActionBusy(false);
        }
    };

    const openStackLink = async (kind) => {
        setOpeningLink(kind);
        setError('');
        try {
            const payload = await getClusterStackLinks(CENTRAL_NAMESPACE, { clusterId });
            const link = (payload?.links || []).find((entry) => entry.kind === kind);
            if (!link?.available || !link.local_port) {
                setError(link?.message || `${kind} dashboard is not available`);
                return;
            }
            const host = window.location.hostname;
            let target = `http://${host}:${link.local_port}`;
            if (kind === 'grafana') {
                const path = link.dashboard_path || GRAFANA_PERFORMANCE_DASHBOARD;
                const params = new URLSearchParams();
                if (deployment.namespace) params.set('var-namespace', deployment.namespace);
                const query = params.toString();
                target = `http://${host}:${link.local_port}${path}${query ? `?${query}` : ''}`;
            } else if (link.dashboard_path) {
                target = `http://${host}:${link.local_port}${link.dashboard_path}`;
            }
            window.open(target, '_blank', 'noopener,noreferrer');
        } catch (failure) {
            setError(failure.message || `Unable to open ${kind} dashboard`);
        } finally {
            setOpeningLink('');
        }
    };

    const busy = actionBusy || Boolean(openingLink);
    const installEnabled = status !== 'enabled';

    const openProfiling = () => {
        setOpen(false);
        onNavigate?.('cluster-monitoring-stack', { clusterId, profileDeployment: deployment });
    };

    return (
        <div className="relative inline-block">
            <button
                ref={buttonRef}
                type="button"
                onClick={toggleOpen}
                disabled={!clusterId}
                aria-haspopup="menu"
                aria-expanded={open}
                title={clusterId ? 'Deployment monitoring' : 'No cluster associated with this deployment'}
                aria-label="Deployment monitoring"
                className={`rounded-lg p-1.5 transition disabled:cursor-not-allowed disabled:opacity-30 ${TONE_CLASSES[status] || TONE_CLASSES.neutral}`}
            >
                {loadingStatus ? <Loader2 size={14} className="animate-spin" /> : <Webcam size={14} />}
            </button>

            {open && createPortal(
                <>
                    <div className="fixed inset-0 z-[290]" onClick={() => setOpen(false)} />
                    <div
                        className="fixed z-[300] w-48 overflow-hidden rounded-lg border border-slate-700/70 bg-slate-900 shadow-xl"
                        style={{ top: (menuPosition?.top || 0) + 6, right: menuPosition?.right || 0 }}
                        role="menu"
                    >
                        {loadingStatus && (
                            <div className="flex items-center gap-2 px-3 py-2 text-xs text-slate-400">
                                <Loader2 size={12} className="animate-spin" /> Loading status…
                            </div>
                        )}
                        {!loadingStatus && error && (
                            <div className="px-3 py-2 text-[11px] text-rose-300">{error}</div>
                        )}
                        {!loadingStatus && !error && status === 'enabled' && (
                            <>
                                <MenuItem
                                    onClick={() => { setOpen(false); openStackLink('grafana'); }}
                                    disabled={busy}
                                    icon={<BarChart3 size={14} className="shrink-0 text-blue-300" />}
                                    label="Open Grafana"
                                />
                                <MenuItem
                                    onClick={() => { setOpen(false); openStackLink('prometheus'); }}
                                    disabled={busy}
                                    icon={<Activity size={14} className="shrink-0 text-orange-300" />}
                                    label="Open Prometheus"
                                />
                                <div className="h-px bg-slate-700/70" />
                                <MenuItem
                                    onClick={openProfiling}
                                    disabled={busy}
                                    icon={<GitBranch size={14} className="shrink-0 text-sky-300" />}
                                    label="Profiling"
                                />
                                <div className="h-px bg-slate-700/70" />
                                <MenuItem
                                    onClick={() => { setOpen(false); runAction('disable'); }}
                                    disabled={busy}
                                    icon={<Pause size={14} className="shrink-0 text-slate-300" />}
                                    label="Disable"
                                />
                                <div className="h-px bg-slate-700/70" />
                                <MenuItem
                                    onClick={() => { setOpen(false); runAction('uninstall'); }}
                                    disabled={busy}
                                    icon={<Trash2 size={14} className="shrink-0 text-rose-400" />}
                                    label="Uninstall"
                                    danger
                                />
                            </>
                        )}
                        {!loadingStatus && !error && status === 'disabled' && (
                            <>
                                <MenuItem
                                    onClick={() => { setOpen(false); runAction('enable'); }}
                                    disabled={busy}
                                    icon={<Play size={14} className="shrink-0 text-emerald-400" />}
                                    label="Enable"
                                />
                                <div className="h-px bg-slate-700/70" />
                                <MenuItem
                                    onClick={() => { setOpen(false); runAction('uninstall'); }}
                                    disabled={busy}
                                    icon={<Trash2 size={14} className="shrink-0 text-rose-400" />}
                                    label="Uninstall"
                                    danger
                                />
                            </>
                        )}
                        {!loadingStatus && !error && status !== 'enabled' && status !== 'disabled' && (
                            <MenuItem
                                onClick={() => { setOpen(false); runAction('install'); }}
                                disabled={!installEnabled || busy}
                                icon={<Rocket size={14} className="shrink-0 text-cyan-400" />}
                                label="Install"
                            />
                        )}
                    </div>
                </>,
                document.body
            )}
        </div>
    );
}

export default DeploymentMonitoringButton;
