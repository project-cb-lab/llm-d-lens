import { useClipboard } from '../../hooks/useClipboard';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
    AlertCircle,
    Check,
    Copy,
    Cpu,
    FileText,
    Loader2,
    Search,
} from 'lucide-react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Input, Label, Select } from '../ui/FormControls';
import {
    getDeploymentExecutionPodLogs,
    getDeploymentExecutionPods,
} from '../OptimizationWorkspace/remoteDeployBackend';
import { deploymentTitle } from './deploymentPresentation';
import { PodStatusBadge } from './PodStatusBadge';

export function DeploymentPodsLogsModal({ deployment, clusterNameById = {}, onClose }) {
    const [pods, setPods] = useState([]);
    const [podsLoading, setPodsLoading] = useState(true);
    const [podsError, setPodsError] = useState('');
    const [podSearchQuery, setPodSearchQuery] = useState('');
    const [selectedNodeFilter, setSelectedNodeFilter] = useState('');
    const [nodeSearchQuery, setNodeSearchQuery] = useState('');
    const [selectedPodName, setSelectedPodName] = useState('');
    const [selectedContainer, setSelectedContainer] = useState('');
    const [tail, setTail] = useState(200);
    const [previous, setPrevious] = useState(false);
    const [logs, setLogs] = useState('');
    const [logsLoading, setLogsLoading] = useState(false);
    const [logsError, setLogsError] = useState('');
    const [filterQuery, setQuery] = useState('');
    const [autoRefresh, setAutoRefresh] = useState(true);
    const { copied, copy } = useClipboard(2000);

    const logContainerRef = useRef(null);
    const executionId = deployment?.execution_id;

    // Fetch pods list
    const fetchPods = async (silent = false) => {
        if (!executionId) {
            setPods([]);
            setPodsLoading(false);
            return;
        }
        if (!silent) setPodsLoading(true);
        try {
            const data = await getDeploymentExecutionPods(executionId);
            const list = Array.isArray(data?.pods) ? data.pods : [];
            setPods(list);
            setPodsError(data?.error || '');
            if (list.length > 0 && (!selectedPodName || !list.some((p) => p.name === selectedPodName))) {
                setSelectedPodName(list[0].name);
            }
        } catch (err) {
            setPodsError(err.message || 'Failed to fetch pods');
            setPods([]);
        } finally {
            if (!silent) setPodsLoading(false);
        }
    };

    useEffect(() => {
        fetchPods();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [executionId]);

    const selectedPod = useMemo(
        () => pods.find((p) => p.name === selectedPodName) || null,
        [pods, selectedPodName],
    );

    const containerOptions = useMemo(() => {
        if (!selectedPod?.containers) return [];
        return selectedPod.containers.map((c) => c.name);
    }, [selectedPod]);

    useEffect(() => {
        if (containerOptions.length > 0 && !containerOptions.includes(selectedContainer)) {
            setSelectedContainer(containerOptions[0]);
        }
    }, [containerOptions, selectedContainer]);

    // Fetch pod logs
    const fetchLogs = async (silent = false) => {
        if (!executionId || !selectedPodName) {
            setLogs('');
            return;
        }
        if (!silent) setLogsLoading(true);
        try {
            const data = await getDeploymentExecutionPodLogs(executionId, selectedPodName, {
                container: selectedContainer || undefined,
                tail,
                previous,
            });
            setLogs(data?.logs || '');
            setLogsError(data?.success === false && !data?.logs ? 'Failed to read logs' : '');
        } catch (err) {
            setLogsError(err.message || 'Failed to fetch logs');
            setLogs('');
        } finally {
            if (!silent) setLogsLoading(false);
        }
    };

    useEffect(() => {
        fetchLogs();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [executionId, selectedPodName, selectedContainer, tail, previous]);

    // Auto-refresh interval (periodically refresh pods and logs regardless of selectedPodName so new pods are discovered)
    useEffect(() => {
        if (!autoRefresh) return undefined;
        const interval = setInterval(() => {
            fetchPods(true);
            if (selectedPodName) {
                fetchLogs(true);
            }
        }, 5000);
        return () => clearInterval(interval);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [autoRefresh, executionId, selectedPodName, selectedContainer, tail, previous]);

    const handleCopy = () => copy(logs);

    const allNodes = useMemo(() => {
        const set = new Set();
        for (const p of pods) {
            if (p.node_name) set.add(p.node_name);
        }
        return [...set].sort();
    }, [pods]);

    const filteredNodes = useMemo(() => {
        if (!nodeSearchQuery.trim()) return allNodes;
        const q = nodeSearchQuery.toLowerCase();
        return allNodes.filter((n) => n.toLowerCase().includes(q));
    }, [allNodes, nodeSearchQuery]);

    const filteredPods = useMemo(() => {
        return pods.filter((p) => {
            if (selectedNodeFilter && p.node_name !== selectedNodeFilter) {
                return false;
            }
            if (podSearchQuery.trim()) {
                const q = podSearchQuery.toLowerCase();
                const matches =
                    p.name?.toLowerCase().includes(q) ||
                    p.pod_ip?.toLowerCase().includes(q) ||
                    p.node_name?.toLowerCase().includes(q) ||
                    p.phase?.toLowerCase().includes(q) ||
                    p.status_reason?.toLowerCase().includes(q);
                if (!matches) return false;
            }
            return true;
        });
    }, [pods, selectedNodeFilter, podSearchQuery]);

    const filteredLogs = useMemo(() => {
        if (!logs) return '';
        if (!filterQuery.trim()) return logs;
        const needle = filterQuery.toLowerCase();
        return logs
            .split('\n')
            .filter((line) => line.toLowerCase().includes(needle))
            .join('\n');
    }, [logs, filterQuery]);

    // Auto-scroll log box to bottom when autoRefresh is active
    useEffect(() => {
        if (autoRefresh && logContainerRef.current) {
            requestAnimationFrame(() => {
                if (logContainerRef.current) {
                    logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
                }
            });
        }
    }, [filteredLogs, autoRefresh]);

    return (
        <Modal
            isOpen
            onClose={onClose}
            title={`Pods & Logs — ${deploymentTitle(deployment)}`}
            subtitle={`Namespace: ${deployment?.namespace || '—'} · Cluster: ${clusterNameById[deployment?.cluster_id] || deployment?.cluster_id || '—'}`}
            variant="drawer"
            size="xl"
        >
            <div className="flex h-full min-h-0 flex-col gap-4">

                {/* Pods List Section */}
                <div className="rounded-xl border border-slate-800/80 bg-slate-900/60 p-3.5 space-y-3 shrink-0">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                            <Cpu className="text-cyan-400" size={16} />
                            <span className="font-semibold text-xs text-white">
                                Pods ({pods.length})
                                {(podSearchQuery.trim() || selectedNodeFilter) && (
                                    <span className="ml-1 text-[11px] text-slate-400 font-normal">
                                        (Filtered: {filteredPods.length})
                                    </span>
                                )}
                            </span>
                        </div>

                        <div className="flex flex-wrap items-center gap-2 flex-1 justify-end ml-auto">
                            {allNodes.length > 0 && (
                                <div className="flex items-center gap-1">
                                    <Select
                                        value={selectedNodeFilter}
                                        onChange={(e) => setSelectedNodeFilter(e.target.value)}
                                        className="h-7 text-xs py-0 w-auto max-w-[150px]"
                                    >
                                        <option value="">All Nodes ({allNodes.length})</option>
                                        {filteredNodes.map((node) => (
                                            <option key={node} value={node}>Node: {node}</option>
                                        ))}
                                    </Select>
                                    {allNodes.length > 3 && (
                                        <div className="relative w-28">
                                            <Search size={11} className="absolute left-2 top-1/2 -translate-y-1/2 text-slate-500" />
                                            <Input
                                                value={nodeSearchQuery}
                                                onChange={(e) => setNodeSearchQuery(e.target.value)}
                                                placeholder="Search node..."
                                                className="pl-6 h-7 text-[11px] bg-slate-950/80 py-0"
                                            />
                                        </div>
                                    )}
                                </div>
                            )}

                            {pods.length > 2 && (
                                <div className="relative w-full max-w-[160px]">
                                    <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500" />
                                    <Input
                                        value={podSearchQuery}
                                        onChange={(e) => setPodSearchQuery(e.target.value)}
                                        placeholder="Search pods..."
                                        className="pl-8 h-7 text-xs bg-slate-950/80 py-0"
                                    />
                                </div>
                            )}
                        </div>
                    </div>

                    {podsError && (
                        <p role="alert" className="text-xs text-rose-400 bg-rose-500/10 border border-rose-500/20 rounded-lg p-2.5">
                            {podsError}
                        </p>
                    )}

                    {podsLoading && pods.length === 0 ? (
                        <div className="flex items-center justify-center py-6 text-xs text-slate-400 gap-2">
                            <Loader2 size={16} className="animate-spin text-cyan-400" />
                            Loading pods...
                        </div>
                    ) : pods.length === 0 ? (
                        <p className="py-4 text-center text-xs text-slate-500">
                            {deployment?.pending || deployment?.status === 'deploying' || deployment?.status === 'rendering'
                                ? 'Deployment is initializing pods in Kubernetes. Auto-refresh is active.'
                                : 'No pods found in namespace.'}
                        </p>
                    ) : filteredPods.length === 0 ? (
                        <p className="py-4 text-center text-xs text-slate-500">
                            No pods matching filter query "{podSearchQuery}".
                        </p>
                    ) : (
                        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2 max-h-24 overflow-y-auto pr-1 p-0.5">
                            {filteredPods.map((pod) => {
                                const isSelected = pod.name === selectedPodName;
                                return (
                                    <div
                                        key={pod.name}
                                        role="button"
                                        tabIndex={0}
                                        onClick={() => setSelectedPodName(pod.name)}
                                        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedPodName(pod.name); } }}
                                        title={`Pod: ${pod.name}\nStatus: ${pod.status_reason || pod.phase || 'Unknown'}${pod.ready ? ' (Ready)' : ''}\nNode: ${pod.node_name || '—'}\nIP: ${pod.pod_ip || '—'}${pod.restarts ? `\nRestarts: ${pod.restarts}` : ''}`}
                                        className={`cursor-pointer rounded-lg border px-2.5 py-1.5 min-h-[44px] transition-all text-xs flex items-center justify-between gap-2 min-w-0 ${
                                            isSelected
                                                ? 'border-cyan-500 bg-cyan-950/40 text-white font-medium ring-1 ring-cyan-500/50 shadow-sm'
                                                : 'border-slate-800/80 bg-slate-950/60 text-slate-300 hover:border-slate-700 hover:bg-slate-800/50'
                                        }`}
                                    >
                                        <div className="flex flex-col min-w-0 flex-1 gap-0.5">
                                            <div className="flex items-center gap-1.5 min-w-0">
                                                <PodStatusBadge phase={pod.phase} reason={pod.status_reason} ready={pod.ready} />
                                                <span className="font-mono text-[11px] truncate font-semibold text-slate-100" title={pod.name}>
                                                    {pod.name}
                                                </span>
                                            </div>
                                            {pod.node_name && (
                                                <div className="font-mono text-[9px] text-slate-400 truncate pl-4" title={`Node: ${pod.node_name}`}>
                                                    {pod.node_name}
                                                </div>
                                            )}
                                        </div>
                                        {pod.restarts > 0 && (
                                            <span className="font-mono text-[10px] text-amber-400 font-bold shrink-0" title={`Restarts: ${pod.restarts}`}>
                                                x{pod.restarts}
                                            </span>
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </div>

                {/* Log Viewer Controls & Terminal */}
                {selectedPodName ? (
                    <div className="flex flex-1 min-h-0 flex-col rounded-xl border border-slate-800/80 bg-slate-900/60 p-3.5 space-y-3">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                            <div className="flex items-center gap-2">
                                <FileText className="text-cyan-400" size={16} />
                                <span className="font-semibold text-xs text-white">
                                    Pod Logs: <code className="font-mono text-cyan-300">{selectedPodName}</code>
                                </span>
                            </div>

                            <div className="flex flex-wrap items-center gap-2">
                                {containerOptions.length > 1 && (
                                    <Select
                                        value={selectedContainer}
                                        onChange={(e) => setSelectedContainer(e.target.value)}
                                        className="h-7 text-xs py-0"
                                    >
                                        {containerOptions.map((c) => (
                                            <option key={c} value={c}>Container: {c}</option>
                                        ))}
                                    </Select>
                                )}

                                <Select
                                    value={tail}
                                    onChange={(e) => setTail(Number(e.target.value))}
                                    className="h-7 text-xs py-0 pl-2.5 pr-7 min-w-[125px] w-auto"
                                >
                                    <option value={100}>100 lines</option>
                                    <option value={200}>200 lines</option>
                                    <option value={500}>500 lines</option>
                                    <option value={1000}>1000 lines</option>
                                </Select>

                                <label className="flex items-center gap-1 text-[11px] text-slate-300 cursor-pointer">
                                    <input
                                        type="checkbox"
                                        checked={previous}
                                        onChange={(e) => setPrevious(e.target.checked)}
                                        className="rounded border-slate-700 bg-slate-900 text-cyan-500 focus:ring-cyan-500"
                                    />
                                    Previous
                                </label>

                                <label className="flex items-center gap-1 text-[11px] text-slate-300 cursor-pointer">
                                    <input
                                        type="checkbox"
                                        checked={autoRefresh}
                                        onChange={(e) => setAutoRefresh(e.target.checked)}
                                        className="rounded border-slate-700 bg-slate-900 text-cyan-500 focus:ring-cyan-500"
                                    />
                                    Auto-refresh (5s)
                                </label>

                                <Button
                                    variant="secondary"
                                    size="sm"
                                    onClick={handleCopy}
                                    disabled={!logs}
                                    className="h-7 text-[11px] gap-1 px-2.5"
                                >
                                    {copied ? <Check size={12} className="text-emerald-400" /> : <Copy size={12} />}
                                    {copied ? 'Copied' : 'Copy'}
                                </Button>
                            </div>
                        </div>

                        {/* Search Filter */}
                        <div className="relative">
                            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                            <Input
                                value={filterQuery}
                                onChange={(e) => setQuery(e.target.value)}
                                placeholder="Filter log lines..."
                                className="pl-9 h-8 text-xs bg-slate-950/80"
                            />
                        </div>

                        {/* Log Text Box */}
                        <div
                            ref={logContainerRef}
                            className="relative flex-1 min-h-[12rem] overflow-y-auto rounded-lg border border-slate-800 bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-200"
                        >
                            {logsLoading && !logs ? (
                                <div className="flex items-center justify-center h-48 text-slate-400 gap-2">
                                    <Loader2 size={16} className="animate-spin text-cyan-400" />
                                    Fetching logs...
                                </div>
                            ) : logsError ? (
                                <div className="flex items-center justify-center h-48 text-rose-400 gap-2">
                                    <AlertCircle size={16} />
                                    {logsError}
                                </div>
                            ) : !filteredLogs ? (
                                <p className="text-slate-500 py-8 text-center">
                                    {logs ? 'No log lines match filter query.' : 'No log entries returned for this pod.'}
                                </p>
                            ) : (
                                <pre className="whitespace-pre-wrap break-words">{filteredLogs}</pre>
                            )}
                        </div>
                    </div>
                ) : (
                    <div className="rounded-xl border border-slate-800/80 bg-slate-900/60 p-6 flex flex-col items-center justify-center text-center gap-2">
                        {podsLoading ? (
                            <div className="flex items-center text-xs text-slate-400 gap-2 py-4">
                                <Loader2 size={18} className="animate-spin text-cyan-400" />
                                Initializing deployment and discovering pods...
                            </div>
                        ) : (
                            <p className="text-xs text-slate-400 py-4">
                                {deployment?.pending || deployment?.status === 'deploying' || deployment?.status === 'rendering'
                                    ? 'Pods are being created in Kubernetes. Auto-refresh is active and will auto-select the pod once available.'
                                    : 'No pods available to display logs.'}
                            </p>
                        )}
                    </div>
                )}
            </div>
        </Modal>
    );
}

export default DeploymentPodsLogsModal;
