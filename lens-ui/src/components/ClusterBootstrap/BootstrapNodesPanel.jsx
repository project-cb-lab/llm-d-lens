import { requestJson } from '../../api/httpClient';
// Kubespray-based "bootstrap a K8s cluster" sub-flow for Step 1 of the
// Create Cluster wizard. See docs/design/CLUSTER_BOOTSTRAP_DESIGN.md at the repo root
// for the full design; this component only talks to
// `/api/cluster/bootstrap/*` and, once a bootstrap job succeeds, hands the
// resulting kubeconfig text back to the caller via `onKubeconfigReady` --
// the wizard's existing Step 1 kubeconfig-file state is otherwise
// untouched (the design's "self-contained pre-step" principle).
import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Info, Loader2, Plus, Trash2 } from 'lucide-react';
import { Button, Input, Label } from '../ui';

const POLL_INTERVAL_MS = 2000;

function newNode(roles = ['worker']) {
    return {
        key: Math.random().toString(36).slice(2),
        // A single IP/hostname, a comma-separated list, or an IP range
        // (`10.0.0.10-20` or `10.0.0.250-10.0.1.5`) -- expanded server-side
        // into individual nodes that all share this row's role selection
        // and SSH credentials. See host_expr.py.
        host: '',
        port: '22',
        roles,
        username: 'root',
        privateKey: '',
        password: '',
    };
}

function toApiNode(node) {
    return {
        host: node.host.trim(),
        port: Number(node.port) || 22,
        roles: node.roles,
        username: node.username.trim() || 'root',
        privateKey: node.privateKey.trim() || null,
        password: node.password.trim() || null,
    };
}

export function BootstrapNodesPanel({ onKubeconfigReady, onCancel }) {
    const [nodes, setNodes] = useState(() => [newNode(['control-plane']), newNode(['worker'])]);
    const [preflightBusy, setPreflightBusy] = useState(false);
    const [hostKeyResults, setHostKeyResults] = useState(null);
    const [hostKeysConfirmed, setHostKeysConfirmed] = useState(false);
    // Flat list of expanded-host results from the last preflight run --
    // each row's `host` spec may expand to several hosts (see
    // docs/design/CLUSTER_BOOTSTRAP_DESIGN.md §3.1), so results aren't 1:1 with rows
    // and are shown as a separate list rather than inline per row.
    const [preflightResults, setPreflightResults] = useState(null); // null | [{host, roles, state, error}]
    const [error, setError] = useState('');
    const [bootstrapId, setBootstrapId] = useState(null);
    const [phase, setPhase] = useState(null); // queued|preflight|provisioning|running|kubeconfig_ready|succeeded|failed|cancelled
    const [logTail, setLogTail] = useState('');
    const [autoRefresh, setAutoRefresh] = useState(true);
    // Kubespray needs http_proxy/https_proxy/no_proxy exported on the
    // *target* nodes themselves (OS package installs + the
    // container-engine/runc role's direct-from-GitHub download) -- distinct
    // from this wizard's later "Network Proxy" step, which only covers the
    // Prism backend's own llm-d/benchmark downloads. "auto" reuses the
    // Prism backend's own proxy env vars (resolved server-side); "custom"
    // lets the operator override that for these target nodes specifically.
    const [proxyMode, setProxyMode] = useState('auto');
    const [httpProxy, setHttpProxy] = useState('');
    const [httpsProxy, setHttpsProxy] = useState('');
    const [noProxy, setNoProxy] = useState('');
    const pollRef = useRef(null);
    const logRef = useRef(null);

    const allPassed = Boolean(preflightResults?.length) && preflightResults.every((item) => item.state === 'passed');
    const hostKeysReady = Boolean(hostKeyResults?.length) && hostKeyResults.every((item) => item.fingerprint);
    const hasControlPlane = nodes.some((node) => node.roles.includes('control-plane'));
    const nodesValid = nodes.length > 0 && hasControlPlane && nodes.every((node) => node.host.trim() && node.roles.length > 0);

    useEffect(() => () => window.clearInterval(pollRef.current), []);

    // Keep the log <pre> pinned to its latest line whenever new output
    // arrives, as long as the "Auto-refresh" checkbox is on -- unchecking
    // it lets the user scroll up to read earlier output without it jumping
    // back to the bottom on the next poll tick.
    useEffect(() => {
        if (!autoRefresh) return;
        const node = logRef.current;
        if (node) node.scrollTop = node.scrollHeight;
    }, [autoRefresh, logTail]);

    const updateNode = (key, patch) => {
        setPreflightResults(null); // stale once rows change; force re-run
        setHostKeyResults(null);
        setHostKeysConfirmed(false);
        setNodes((current) => current.map((node) => (node.key === key ? { ...node, ...patch } : node)));
    };
    const removeNode = (key) => {
        setPreflightResults(null);
        setHostKeyResults(null);
        setHostKeysConfirmed(false);
        setNodes((current) => current.filter((node) => node.key !== key));
    };
    const toggleRole = (key, role) => {
        setPreflightResults(null);
        setHostKeyResults(null);
        setHostKeysConfirmed(false);
        setNodes((current) =>
            current.map((node) => {
                if (node.key !== key) return node;
                const roles = node.roles.includes(role) ? node.roles.filter((existing) => existing !== role) : [...node.roles, role];
                return { ...node, roles };
            }),
        );
    };

    const hostKeyPins = () => (hostKeyResults || []).map(({ host, port, fingerprint }) => ({ host, port, fingerprint }));

    const discoverHostKeys = async () => {
        if (!nodesValid || preflightBusy) return;
        setError('');
        setPreflightBusy(true);
        setPreflightResults(null);
        setHostKeyResults(null);
        setHostKeysConfirmed(false);
        try {
            const payload = await requestJson('/api/cluster/bootstrap/host-keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodes: nodes.map((node) => ({ host: node.host.trim(), port: Number(node.port) || 22 })) }),
            });
            setHostKeyResults(payload.items || []);
        } catch (discoveryError) {
            setError(discoveryError.message);
        } finally {
            setPreflightBusy(false);
        }
    };

    const runPreflight = async () => {
        if (!nodesValid || !hostKeysReady || !hostKeysConfirmed || preflightBusy) return;
        setError('');
        setPreflightBusy(true);
        setPreflightResults(null);
        try {
            const payload = await requestJson('/api/cluster/bootstrap/preflight', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodes: nodes.map(toApiNode), hostKeys: hostKeyPins() }),
            });
            setPreflightResults(payload.items || []);
        } catch (preflightError) {
            setError(preflightError.message);
            setPreflightResults([]);
        } finally {
            setPreflightBusy(false);
        }
    };

    const pollStatus = (id) => {
        window.clearInterval(pollRef.current);
        pollRef.current = window.setInterval(async () => {
            try {
                const payload = await requestJson(`/api/cluster/bootstrap/${id}`, {}, {
                    strictJson: true,
                    errorFactory: (_payload, response) => new Error(`Bootstrap status request failed (${response.status})`),
                });
                setPhase(payload.phase);
                setLogTail(payload.logTail || '');
                if (payload.phase === 'succeeded' && payload.kubeconfig) {
                    window.clearInterval(pollRef.current);
                    onKubeconfigReady(payload.kubeconfig);
                } else if (payload.phase === 'failed' || payload.phase === 'cancelled') {
                    window.clearInterval(pollRef.current);
                    setError(payload.error || `Bootstrap ${payload.phase}`);
                }
            } catch (pollError) {
                window.clearInterval(pollRef.current);
                setError(pollError.message);
            }
        }, POLL_INTERVAL_MS);
    };

    const startBootstrap = async () => {
        if (!allPassed) return;
        setError('');
        try {
            const payload = await requestJson('/api/cluster/bootstrap', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    nodes: nodes.map(toApiNode),
                    hostKeys: hostKeyPins(),
                    proxy:
                        proxyMode === 'custom'
                            ? {
                                  mode: 'custom',
                                  httpProxy: httpProxy.trim() || null,
                                  httpsProxy: httpsProxy.trim() || null,
                                  noProxy: noProxy.trim() || null,
                              }
                            : { mode: 'auto' },
                }),
            });
            setBootstrapId(payload.bootstrapId);
            setPhase('queued');
            pollStatus(payload.bootstrapId);
        } catch (startError) {
            setError(startError.message);
        }
    };

    const cancelBootstrap = async () => {
        window.clearInterval(pollRef.current);
        if (bootstrapId) {
            try {
                await requestJson(`/api/cluster/bootstrap/${bootstrapId}/cancel`, { method: 'POST' });
            } catch {
                /* best-effort */
            }
        }
        onCancel();
    };

    if (bootstrapId) {
        const phaseLabel = phase === 'provisioning' ? 'provisioning Kubespray (first run only, may take a few minutes)…' : phase || 'queued';
        return (
            <div className="space-y-3 rounded-lg border border-slate-700/70 bg-slate-800/40 p-3">
                <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 text-sm font-medium text-theme-text">
                        {phase !== 'failed' && phase !== 'cancelled' && <Loader2 className="h-4 w-4 animate-spin text-sky-400" />}
                        Bootstrapping the cluster via Kubespray… (phase: {phaseLabel})
                    </div>
                    <label className="flex shrink-0 items-center gap-1.5 text-xs font-medium text-slate-400">
                        <input
                            type="checkbox"
                            className="h-3.5 w-3.5 rounded border-slate-600 bg-slate-900 text-sky-500 focus:ring-sky-500"
                            checked={autoRefresh}
                            onChange={(event) => setAutoRefresh(event.target.checked)}
                        />
                        Auto-refresh
                    </label>
                </div>
                <pre ref={logRef} className="max-h-48 overflow-auto rounded-md bg-black/60 p-2 text-[11px] text-slate-300">{logTail || 'Waiting for ansible-playbook output…'}</pre>
                {error && (
                    <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {error}
                    </div>
                )}
                <Button type="button" variant="secondary" onClick={cancelBootstrap}>Cancel</Button>
            </div>
        );
    }

    return (
        <div className="space-y-3 rounded-lg border border-slate-700/70 bg-slate-800/40 p-3">
            <p className="text-xs text-theme-muted">
                Prism will run Kubespray's <code>cluster.yml</code> playbook over SSH to install a production-ready
                Kubernetes cluster on the nodes below, then use the resulting kubeconfig for this cluster. Credentials
                are only kept in memory for the duration of the bootstrap job.
            </p>
            <div className="space-y-2">
                {nodes.map((node) => (
                    <div key={node.key} className="grid grid-cols-12 items-start gap-2 rounded-md border border-slate-700/50 p-2">
                        <div className="col-span-2">
                            <Label className="text-[11px]">Host(s)</Label>
                            <Input
                                value={node.host}
                                onChange={(event) => updateNode(node.key, { host: event.target.value })}
                                placeholder="10.0.0.1, or 10.0.0.10-20"
                            />
                            <p className="mt-0.5 text-[10px] text-theme-muted">
                                Single IP, comma-separated list, or a range (e.g. <code>10.0.0.10-20</code>) -- each expands to its own node.
                            </p>
                        </div>
                        <div className="col-span-1">
                            <Label className="text-[11px]">Port</Label>
                            <Input value={node.port} onChange={(event) => updateNode(node.key, { port: event.target.value })} />
                        </div>
                        <div className="col-span-2">
                            <Label className="text-[11px]">Role(s)</Label>
                            <div className="flex flex-col gap-0.5 text-[11px]">
                                <label className="flex items-center gap-1">
                                    <input type="checkbox" checked={node.roles.includes('control-plane')} onChange={() => toggleRole(node.key, 'control-plane')} />
                                    control-plane
                                </label>
                                <label className="flex items-center gap-1">
                                    <input type="checkbox" checked={node.roles.includes('worker')} onChange={() => toggleRole(node.key, 'worker')} />
                                    worker
                                </label>
                            </div>
                        </div>
                        <div className="col-span-2">
                            <Label className="flex items-center gap-1 text-[11px]">
                                Username
                                <span
                                    className="inline-flex cursor-help"
                                    title="The SSH user must be able to run 'sudo' without a password prompt (passwordless sudo, e.g. NOPASSWD in /etc/sudoers), or be root. Kubespray needs root privileges on every node to install containerd/kubelet/kubeadm, tune kernel parameters, and write to /etc/kubernetes."
                                >
                                    <Info className="h-3 w-3 shrink-0 text-theme-muted" />
                                </span>
                            </Label>
                            <Input value={node.username} onChange={(event) => updateNode(node.key, { username: event.target.value })} />
                        </div>
                        <div className="col-span-2">
                            <Label className="text-[11px]">SSH private key</Label>
                            <textarea
                                className="h-9 w-full resize-none overflow-hidden rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-theme-text placeholder:text-theme-muted transition-colors focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40 dark:border-slate-700 dark:bg-slate-900"
                                rows={1}
                                value={node.privateKey}
                                onChange={(event) => updateNode(node.key, { privateKey: event.target.value, password: '' })}
                                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                            />
                        </div>
                        <div className="col-span-2">
                            <Label className="text-[11px]">or SSH password</Label>
                            <Input
                                type="password"
                                value={node.password}
                                onChange={(event) => updateNode(node.key, { password: event.target.value, privateKey: '' })}
                                placeholder="password"
                            />
                        </div>
                        <div className="col-span-1 flex items-end justify-end">
                            <button type="button" onClick={() => removeNode(node.key)} className="rounded p-1 text-rose-400 hover:bg-rose-500/10" aria-label="Remove node">
                                <Trash2 className="h-4 w-4" />
                            </button>
                        </div>
                    </div>
                ))}
            </div>
            <Button
                type="button"
                variant="secondary"
                onClick={() => {
                    setHostKeyResults(null);
                    setHostKeysConfirmed(false);
                    setPreflightResults(null);
                    setNodes((current) => [...current, newNode()]);
                }}
            >
                <Plus className="mr-1 h-4 w-4" /> Add node
            </Button>

            {hostKeyResults && (
                <div className="space-y-2 rounded-md border border-slate-700/50 p-2 text-[11px]">
                    <div className="font-medium text-theme-text">SSH host keys</div>
                    {hostKeyResults.map((item) => (
                        <div key={`${item.host}:${item.port}`} className="grid grid-cols-[minmax(7rem,0.3fr)_minmax(0,1fr)] gap-2">
                            <span className="text-theme-text">{item.host}:{item.port}</span>
                            {item.fingerprint ? (
                                <span className="break-all font-mono text-theme-muted">{item.algorithm} {item.fingerprint}</span>
                            ) : (
                                <span className="text-rose-300">{item.error || 'Fingerprint unavailable'}</span>
                            )}
                        </div>
                    ))}
                    {hostKeysReady && (
                        <label className="flex items-start gap-2 border-t border-slate-700/50 pt-2 text-theme-text">
                            <input
                                type="checkbox"
                                checked={hostKeysConfirmed}
                                onChange={(event) => {
                                    setHostKeysConfirmed(event.target.checked);
                                    setPreflightResults(null);
                                }}
                            />
                            I have independently verified these fingerprints and trust these SSH hosts.
                        </label>
                    )}
                </div>
            )}

            <div className="space-y-2 rounded-md border border-slate-700/50 p-2">
                <Label className="flex items-center gap-1 text-[11px]">
                    Network proxy for target nodes
                    <span
                        className="inline-flex cursor-help"
                        title="Kubespray needs http_proxy/https_proxy/no_proxy exported on each node to install OS packages and download container-engine binaries (e.g. runc) directly from GitHub. Required if these nodes have no direct internet route."
                    >
                        <Info className="h-3 w-3 shrink-0 text-theme-muted" />
                    </span>
                </Label>
                <div className="flex flex-col gap-1 text-[11px] text-theme-muted">
                    <label className="flex items-center gap-1.5">
                        <input type="radio" name="bootstrap-proxy-mode" checked={proxyMode === 'auto'} onChange={() => setProxyMode('auto')} />
                        Auto (reuse whatever proxy is already configured on each target node itself)
                    </label>
                    <label className="flex items-center gap-1.5">
                        <input type="radio" name="bootstrap-proxy-mode" checked={proxyMode === 'custom'} onChange={() => setProxyMode('custom')} />
                        Custom for these nodes
                    </label>
                </div>
                {proxyMode === 'custom' && (
                    <div className="grid grid-cols-3 gap-2 pl-5">
                        <div>
                            <Label className="text-[11px]">HTTP_PROXY</Label>
                            <Input value={httpProxy} onChange={(event) => setHttpProxy(event.target.value)} placeholder="http://proxy:3128" />
                        </div>
                        <div>
                            <Label className="text-[11px]">HTTPS_PROXY</Label>
                            <Input value={httpsProxy} onChange={(event) => setHttpsProxy(event.target.value)} placeholder="http://proxy:3128" />
                        </div>
                        <div>
                            <Label className="text-[11px]">NO_PROXY</Label>
                            <Input value={noProxy} onChange={(event) => setNoProxy(event.target.value)} placeholder="localhost,.svc" />
                        </div>
                    </div>
                )}
            </div>

            {preflightResults && preflightResults.length > 0 && (
                <div className="space-y-2 rounded-md border border-slate-700/50 p-2 text-[11px]">
                    <div className="font-medium text-theme-text">Preflight results ({preflightResults.length} host{preflightResults.length === 1 ? '' : 's'})</div>
                    {preflightResults.map((item) => (
                        <div key={item.host} className="space-y-1 rounded border border-slate-700/40 p-1.5">
                            <div className="flex items-center gap-2">
                                <span className="w-36 shrink-0 truncate text-theme-text">{item.host}</span>
                                <span className="w-32 shrink-0 text-theme-muted">{item.roles.join(' + ')}</span>
                                {item.state === 'passed' && <span className="text-emerald-400">passed</span>}
                                {item.state === 'failed' && <span className="text-rose-400">failed</span>}
                                {(item.state === 'pending' || item.state === 'running') && <span className="text-sky-400">{item.state}…</span>}
                            </div>
                            {item.checks && item.checks.length > 0 && (
                                <ul className="ml-2 space-y-0.5 border-l border-slate-700/40 pl-2">
                                    {item.checks.map((check) => (
                                        <li key={check.id} className="flex items-start gap-1.5">
                                            {check.status === 'passed' && <span className="text-emerald-400">✓</span>}
                                            {check.status === 'failed' && <span className="text-rose-400">✗</span>}
                                            {check.status === 'skipped' && <span className="text-theme-muted">–</span>}
                                            <span className="text-theme-text">{check.label}:</span>
                                            <span className={check.status === 'failed' ? 'text-rose-300' : 'text-theme-muted'}>
                                                {check.detail}
                                            </span>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </div>
                    ))}
                </div>
            )}

            {error && (
                <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {error}
                </div>
            )}

            <div className="flex items-center gap-2">
                <Button type="button" variant="secondary" disabled={!nodesValid || preflightBusy} onClick={discoverHostKeys}>
                    {!hostKeyResults && preflightBusy && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
                    Discover SSH fingerprints
                </Button>
                {hostKeysReady && (
                    <Button type="button" variant="secondary" disabled={!hostKeysConfirmed || preflightBusy} onClick={runPreflight}>
                        Run preflight
                    </Button>
                )}
                <Button type="button" disabled={!allPassed || !hostKeysConfirmed} onClick={startBootstrap}>
                    Start bootstrap
                </Button>
                <Button type="button" variant="ghost" onClick={onCancel}>
                    Back to kubeconfig upload
                </Button>
            </div>
        </div>
    );
}
