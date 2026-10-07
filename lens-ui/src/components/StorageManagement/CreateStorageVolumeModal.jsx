import { useSubmission } from '../../hooks/useSubmission';
import { FormError } from '../ui/FormError';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Checkbox, Input, Label, Select } from '../ui/FormControls';
import { listStorageClasses, listStorageNodes } from './storageManagementBackend';
import { PURPOSE_OPTIONS, purposeLabel } from './storagePresentation';

const CAPACITY_PATTERN = /^\d+(Gi|Mi|Ti)$/;
const NAME_LIMIT = 63;
const KINDS = [
    { value: 'local-disk', label: 'hostPath' },
    { value: 'nfs', label: 'NFS' },
    { value: 'dynamic-pvc', label: 'Dynamic PVC' },
];

export function CreateStorageVolumeModal({ clusters = [], defaultClusterId = '', onCancel, onCreate }) {
    const [clusterId, setClusterId] = useState(defaultClusterId);
    const [kind, setKind] = useState('local-disk');
    const [name, setName] = useState('');
    const [capacity, setCapacity] = useState('100Gi');
    const [readOnly, setReadOnly] = useState(true);
    const [purposes, setPurposes] = useState([]);
    const [hostPath, setHostPath] = useState('');
    const [nfsServer, setNfsServer] = useState('');
    const [nfsPath, setNfsPath] = useState('');
    const [storageClass, setStorageClass] = useState('');
    const [namespace, setNamespace] = useState(defaultClusterId ? `llm-d-bench-storage-${defaultClusterId}` : 'llm-d-bench-storage');
    const [namespaceTouched, setNamespaceTouched] = useState(false);
    const [accessMode, setAccessMode] = useState('ReadWriteOnce');
    const [nodes, setNodes] = useState([]);
    const [storageClasses, setStorageClasses] = useState([]);
    const { pending: creating, error, setError, run } = useSubmission('Failed to create storage volume', { keepPendingOnSuccess: true });
    const firstFieldRef = useRef(null);

    useEffect(() => {
        firstFieldRef.current?.focus();
    }, []);

    useEffect(() => {
        if (!clusterId) {
            setNodes([]);
            setStorageClasses([]);
            return undefined;
        }
        const controller = new AbortController();
        (async () => {
            try {
                if (kind === 'local-disk') {
                    const items = await listStorageNodes({ clusterId, signal: controller.signal });
                    // Control-plane-only nodes (tainted, not also acting as a
                    // worker) never actually run Pods -- Model Cache download
                    // Jobs and Deploy workloads both skip them (see
                    // model_cache/jobs.py::_target_nodes). Don't ask the user
                    // to replicate the hostPath onto a node nothing will ever
                    // be scheduled onto; a single-node dev cluster whose sole
                    // node is both control-plane and worker still shows up
                    // (schedulable stays true once its taint is removed).
                    setNodes(items.filter((node) => node.schedulable !== false));
                } else if (kind === 'dynamic-pvc') {
                    const items = await listStorageClasses({ clusterId, signal: controller.signal });
                    setStorageClasses(items);
                    if (!storageClass && items.length) {
                        setStorageClass(items.find((item) => item.isDefault)?.name || items[0].name);
                    }
                }
            } catch {
                // Non-fatal: fields fall back to free text entry.
            }
        })();
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [clusterId, kind]);

    useEffect(() => {
        if (namespaceTouched) return;
        setNamespace(clusterId ? `llm-d-bench-storage-${clusterId}` : 'llm-d-bench-storage');
    }, [clusterId, namespaceTouched]);

    const capacityValid = CAPACITY_PATTERN.test(capacity.trim());
    const nameValid = !name.trim() || name.trim().length <= NAME_LIMIT;
    const modelCacheSelected = purposes.includes('model-cache');
    const modelCacheDynamicPvcInvalid = modelCacheSelected && kind === 'dynamic-pvc';

    const kindSpecValid = useMemo(() => {
        if (kind === 'local-disk') return hostPath.trim().startsWith('/');
        if (kind === 'nfs') return Boolean(nfsServer.trim() && nfsPath.trim().startsWith('/'));
        if (kind === 'dynamic-pvc') return Boolean(storageClass.trim() && namespace.trim());
        return false;
    }, [kind, hostPath, nfsServer, nfsPath, storageClass, namespace]);

    const invalid = !clusterId || !capacityValid || !nameValid || !kindSpecValid || modelCacheDynamicPvcInvalid;

    const setPurpose = (purpose) => {
        const next = purpose ? [purpose] : [];
        // Model Cache downloads models onto the volume, so a volume tagged
        // for that purpose must be writable -- read-only would make every
        // download Job fail with a permission error at mount time.
        if (purpose === 'model-cache') {
            setReadOnly(false);
        }
        setPurposes(next);
    };

    const submit = async (event) => {
        event.preventDefault();
        if (modelCacheDynamicPvcInvalid) {
            setError('Model Cache storage cannot use the Dynamic PVC type. Choose hostPath or NFS.');
            return;
        }
        if (invalid || creating) return;
        await run(async () => {
            const payload = {
                clusterId,
                name: name.trim(),
                kind,
                capacity: capacity.trim(),
                readOnly,
                purposes,
                ...(kind === 'local-disk' ? { localDisk: { hostPath: hostPath.trim() } } : {}),
                ...(kind === 'nfs' ? { nfs: { server: nfsServer.trim(), path: nfsPath.trim() } } : {}),
                ...(kind === 'dynamic-pvc'
                    ? { dynamicPvc: { storageClass: storageClass.trim(), namespace: namespace.trim(), accessMode } }
                    : {}),
            };
            await onCreate(payload);
        });
    };

    return (
        <Modal
            isOpen
            onClose={creating ? undefined : onCancel}
            title="New storage volume"
            subtitle="Register a reusable volume that Deployments can mount as a model cache."
            size="lg"
            closeOnBackdrop={!creating}
            closeOnEscape={!creating}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={creating}>Cancel</Button>
                    <Button variant="sky" type="submit" form="create-storage-volume-form" isLoading={creating} disabled={invalid}>
                        {creating ? 'Creating' : 'Create storage volume'}
                    </Button>
                </>
            }
        >
            <form id="create-storage-volume-form" onSubmit={submit} className="flex flex-col gap-4">
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <div>
                        <Label htmlFor="storage-cluster">Cluster</Label>
                        <Select
                            id="storage-cluster"
                            ref={firstFieldRef}
                            value={clusterId}
                            onChange={(event) => setClusterId(event.target.value)}
                            disabled={creating}
                        >
                            <option value="">Select a cluster</option>
                            {clusters.map((cluster) => (
                                <option key={cluster.id} value={cluster.id}>{cluster.name || cluster.id}</option>
                            ))}
                        </Select>
                    </div>
                    <div>
                        <Label htmlFor="storage-kind">Type</Label>
                        <Select id="storage-kind" value={kind} onChange={(event) => setKind(event.target.value)} disabled={creating}>
                            {KINDS.map((option) => (
                                <option key={option.value} value={option.value} disabled={modelCacheSelected && option.value === 'dynamic-pvc'}>{option.label}</option>
                            ))}
                        </Select>
                        {modelCacheSelected && (
                            <p className="mt-1.5 text-[11px] leading-relaxed text-amber-300">
                                Model Cache storage supports hostPath or NFS only.
                            </p>
                        )}
                    </div>
                </div>

                {kind === 'local-disk' && (
                    <div className="flex flex-col gap-3">
                        <div>
                            <Label htmlFor="storage-host-path">Host path</Label>
                            <Input id="storage-host-path" value={hostPath} placeholder="/data/models" onChange={(event) => setHostPath(event.target.value.trim())} disabled={creating} />
                            <p className="mt-1.5 text-[11px] leading-relaxed text-slate-400">
                                This directory must exist with the same content on <strong>every node</strong> in the
                                cluster (e.g. a pre-populated or synced model cache). Prism does not pin the volume to
                                a single node — Deployments using it may be scheduled onto any node below.
                            </p>
                        </div>
                        {nodes.length > 0 && (
                            <div className="rounded-lg border border-slate-800/60 bg-slate-900/40 px-3 py-2.5">
                                <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Cluster nodes</div>
                                <div className="mt-1.5 flex flex-wrap gap-1.5">
                                    {nodes.map((node) => (
                                        <span
                                            key={node.name}
                                            className={`rounded-full border px-2 py-0.5 text-[11px] ${node.ready ? 'border-slate-700 text-slate-300' : 'border-amber-500/30 text-amber-300'}`}
                                            title={node.ready ? 'Ready' : 'Not ready'}
                                        >
                                            {node.name}
                                        </span>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                )}

                {kind === 'nfs' && (
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                        <div>
                            <Label htmlFor="storage-nfs-server">Server</Label>
                            <Input id="storage-nfs-server" value={nfsServer} placeholder="nfs.example.com" onChange={(event) => setNfsServer(event.target.value.trim())} disabled={creating} />
                        </div>
                        <div>
                            <Label htmlFor="storage-nfs-path">Export path</Label>
                            <Input id="storage-nfs-path" value={nfsPath} placeholder="/export/models" onChange={(event) => setNfsPath(event.target.value.trim())} disabled={creating} />
                        </div>
                    </div>
                )}

                {kind === 'dynamic-pvc' && (
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                        <div>
                            <Label htmlFor="storage-class">StorageClass</Label>
                            {storageClasses.length ? (
                                <Select id="storage-class" value={storageClass} onChange={(event) => setStorageClass(event.target.value)} disabled={creating}>
                                    <option value="">Select a StorageClass</option>
                                    {storageClasses.map((item) => (
                                        <option key={item.name} value={item.name}>{item.name}{item.isDefault ? ' (default)' : ''}</option>
                                    ))}
                                </Select>
                            ) : (
                                <Input id="storage-class" value={storageClass} placeholder="standard" onChange={(event) => setStorageClass(event.target.value.trim())} disabled={creating} />
                            )}
                        </div>
                        <div>
                            <Label htmlFor="storage-namespace">Namespace</Label>
                            <Input id="storage-namespace" value={namespace} onChange={(event) => { setNamespaceTouched(true); setNamespace(event.target.value.trim()); }} disabled={creating} />
                        </div>
                        <div>
                            <Label htmlFor="storage-access-mode">Access mode</Label>
                            <Select id="storage-access-mode" value={accessMode} onChange={(event) => setAccessMode(event.target.value)} disabled={creating}>
                                <option value="ReadWriteOnce">ReadWriteOnce</option>
                                <option value="ReadWriteMany">ReadWriteMany</option>
                                <option value="ReadOnlyMany">ReadOnlyMany</option>
                            </Select>
                        </div>
                    </div>
                )}

                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <div>
                        <Label htmlFor="storage-name">Name (optional)</Label>
                        <Input
                            id="storage-name"
                            value={name}
                            placeholder="qwen-cache-node1"
                            maxLength={NAME_LIMIT}
                            error={!nameValid}
                            onChange={(event) => setName(event.target.value)}
                            disabled={creating}
                        />
                    </div>
                    <div>
                        <Label htmlFor="storage-capacity">Capacity</Label>
                        <Input
                            id="storage-capacity"
                            value={capacity}
                            placeholder="100Gi"
                            error={!capacityValid}
                            onChange={(event) => setCapacity(event.target.value.trim())}
                            disabled={creating}
                        />
                        {kind === 'nfs' && (
                            <p className="mt-1 text-[10px] text-slate-500">Declared value only, not an enforced quota.</p>
                        )}
                    </div>
                </div>

                <div className="rounded-lg border border-slate-700/70 bg-slate-800/40 px-3 py-2.5">
                    <Checkbox
                        id="storage-read-only"
                        checked={readOnly}
                        onChange={(event) => setReadOnly(event.target.checked)}
                        disabled={creating || modelCacheSelected}
                        label="Read-only when mounted"
                    />
                    <p className="mt-1.5 pl-6 text-[11px] leading-relaxed text-slate-400">
                        {modelCacheSelected
                            ? 'Disabled because Model Cache needs to write downloaded model files onto this volume.'
                            : 'Recommended for shared model caches so multiple Deployments can mount the same volume safely.'}
                    </p>
                </div>

                <div className="rounded-lg border border-slate-700/70 bg-slate-800/40 px-3 py-2.5">
                    <Label>Purpose (optional)</Label>
                    <div className="mt-1.5 space-y-1.5">
                        <label className="flex items-center gap-2 text-sm text-slate-200">
                            <input
                                type="radio"
                                name="storage-purpose"
                                checked={!purposes[0]}
                                onChange={() => setPurpose('')}
                                disabled={creating}
                            />
                            General
                        </label>
                        {PURPOSE_OPTIONS.map((purpose) => (
                            <label key={purpose} className="flex items-center gap-2 text-sm text-slate-200">
                                <input
                                    type="radio"
                                    name="storage-purpose"
                                    checked={purposes[0] === purpose}
                                    onChange={() => setPurpose(purpose)}
                                    disabled={creating || (purpose === 'model-cache' && kind === 'dynamic-pvc')}
                                />
                                {purposeLabel(purpose)}
                            </label>
                        ))}
                    </div>
                    <p className="mt-1.5 text-[11px] leading-relaxed text-slate-400">
                        Tag this volume so it appears as a candidate in matching workflows, e.g. Model Cache.
                    </p>
                    {kind === 'dynamic-pvc' && (
                        <p className="mt-1.5 text-[11px] leading-relaxed text-amber-300">
                            Dynamic PVC cannot be used for Model Cache purpose.
                        </p>
                    )}
                </div>

                <FormError message={error} />
            </form>
        </Modal>
    );
}

export default CreateStorageVolumeModal;
