import { useSubmission } from '../../hooks/useSubmission';
import { FormError } from '../ui/FormError';
import { useEffect, useMemo, useState } from 'react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Input, Label, Select } from '../ui/FormControls';
import { listStorageVolumes } from '../StorageManagement/storageManagementBackend';
import { HuggingFaceModelPickerModal } from './HuggingFaceModelPickerModal';
import { ModelMarketPickerModal } from './ModelMarketPickerModal';
import { MODELS } from '../../data/modelCatalog';

const REPO_ID_PATTERN = /^[\w.-]+\/[\w.-]+$/;
const MODEL_SOURCES = [
    { value: 'huggingface', label: 'Hugging Face' },
    { value: 'model-market', label: 'Model Market' },
];

export function DownloadModelModal({ clusters = [], defaultClusterId = '', defaultStorageVolumeId = '', onCancel, onCreate }) {
    const [clusterId, setClusterId] = useState(defaultClusterId);
    const [storageVolumeId, setStorageVolumeId] = useState(defaultStorageVolumeId);
    const [volumes, setVolumes] = useState([]);
    const [repoId, setRepoId] = useState('');
    const [revision, setRevision] = useState('main');
    const [modelSource, setModelSource] = useState('huggingface');
    const [selectedMarketModelId, setSelectedMarketModelId] = useState('');
    const [pickerOpen, setPickerOpen] = useState(false);
    const [marketPickerOpen, setMarketPickerOpen] = useState(false);
    const [selectedModelTags, setSelectedModelTags] = useState(null);
    const { pending: creating, error, run } = useSubmission('Failed to start model download', { keepPendingOnSuccess: true });

    useEffect(() => {
        if (!clusterId) {
            setVolumes([]);
            return undefined;
        }
        const controller = new AbortController();
        (async () => {
            try {
                const items = await listStorageVolumes({ clusterId, status: 'ready', purpose: 'model-cache', signal: controller.signal });
                setVolumes(items);
                if (storageVolumeId && !items.some((item) => item.id === storageVolumeId)) setStorageVolumeId('');
            } catch {
                setVolumes([]);
            }
        })();
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [clusterId]);


    const repoIdValid = REPO_ID_PATTERN.test(repoId.trim());
    const invalid = !clusterId || !storageVolumeId || !repoIdValid;

    const selectedVolume = useMemo(() => volumes.find((item) => item.id === storageVolumeId), [volumes, storageVolumeId]);
    const selectedMarketModel = useMemo(
        () => MODELS.find((item) => item.id === selectedMarketModelId) || null,
        [selectedMarketModelId],
    );

    const handleSourceChange = (newSource) => {
        setModelSource(newSource);
        setSelectedModelTags(null);
        setRevision('main');
        if (newSource === 'model-market') {
            setSelectedMarketModelId('');
            setRepoId('');
            setMarketPickerOpen(true);
        } else {
            setSelectedMarketModelId('');
            setRepoId('');
        }
    };

    const handleMarketModelSelect = (marketId) => {
        setSelectedMarketModelId(marketId);
        const model = MODELS.find((item) => item.id === marketId);
        if (model) {
            setRepoId(model.repository);
            setRevision('main');
        } else {
            setRepoId('');
        }
    };

    const submit = async (event) => {
        event.preventDefault();
        if (invalid || creating) return;
        await run(async () => {
            const payload = {
                clusterId,
                storageVolumeId,
                source: {
                    kind: 'huggingface',
                    huggingface: { repoId: repoId.trim(), revision: revision.trim() || 'main' },
                },
            };
            await onCreate(payload);
        });
    };

    return (
        <Modal
            isOpen
            onClose={creating ? undefined : onCancel}
            title="Download a model"
            subtitle="Fetch a model into a storage volume so Deployments can use it as a model cache."
            size="lg"
            closeOnBackdrop={!creating}
            closeOnEscape={!creating}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={creating}>Cancel</Button>
                    <Button variant="sky" type="submit" form="download-model-form" isLoading={creating} disabled={invalid}>
                        {creating ? 'Starting' : 'Start download'}
                    </Button>
                </>
            }
        >
            <form id="download-model-form" onSubmit={submit} className="flex flex-col gap-4">
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <div>
                        <Label htmlFor="model-cache-cluster">Cluster</Label>
                        <Select id="model-cache-cluster" value={clusterId} onChange={(event) => setClusterId(event.target.value)} disabled={creating}>
                            <option value="">Select a cluster</option>
                            {clusters.map((cluster) => (
                                <option key={cluster.id} value={cluster.id}>{cluster.name || cluster.id}</option>
                            ))}
                        </Select>
                    </div>
                    <div>
                        <Label htmlFor="model-cache-volume">Storage volume</Label>
                        <Select
                            id="model-cache-volume"
                            value={storageVolumeId}
                            onChange={(event) => setStorageVolumeId(event.target.value)}
                            disabled={creating || !clusterId}
                        >
                            <option value="">Select a storage volume</option>
                            {volumes.map((volume) => (
                                <option key={volume.id} value={volume.id}>{volume.name || volume.id}</option>
                            ))}
                        </Select>
                        {clusterId && volumes.length === 0 && (
                            <p className="mt-1.5 text-[11px] text-amber-300">
                                No ready storage volumes on this cluster. Register one on the Storage page first.
                            </p>
                        )}
                    </div>
                </div>

                <div>
                    <Label htmlFor="model-cache-source">Model source</Label>
                    <div className="flex items-center gap-2">
                        <Select
                            id="model-cache-source"
                            value={modelSource}
                            onChange={(event) => handleSourceChange(event.target.value)}
                            disabled={creating}
                            className="max-w-[220px]"
                        >
                            {MODEL_SOURCES.map((option) => (
                                <option key={option.value} value={option.value}>{option.label}</option>
                            ))}
                        </Select>
                        {modelSource === 'huggingface' && (
                            <Button type="button" variant="secondary" onClick={() => setPickerOpen(true)} disabled={creating}>
                                Browse Hugging Face Hub
                            </Button>
                        )}
                        {modelSource === 'model-market' && (
                            <Button type="button" variant="secondary" onClick={() => setMarketPickerOpen(true)} disabled={creating}>
                                Browse Model Market
                            </Button>
                        )}
                    </div>
                </div>

                {modelSource === 'model-market' ? (
                    <div className="flex flex-col gap-3">
                        {selectedMarketModel ? (
                            <div className="rounded-lg border border-slate-700/80 bg-slate-900/60 p-3 text-xs">
                                <div className="flex items-center justify-between gap-2">
                                    <div className="flex items-center gap-2">
                                        <span className="font-semibold text-white">{selectedMarketModel.name}</span>
                                        <span className="rounded-full border border-cyan-400/30 bg-cyan-400/10 px-2 py-0.5 text-[10px] text-cyan-200">
                                            {selectedMarketModel.category}
                                        </span>
                                    </div>
                                    <Button
                                        type="button"
                                        variant="secondary"
                                        onClick={() => setMarketPickerOpen(true)}
                                        disabled={creating}
                                        className="h-7 px-2.5 text-[11px]"
                                    >
                                        Change model
                                    </Button>
                                </div>
                                <div className="mt-2 grid grid-cols-2 gap-2 text-[11px] text-slate-400">
                                    <div><span className="text-slate-500">Repository:</span> <code className="font-mono text-slate-300">{selectedMarketModel.repository}</code></div>
                                    <div><span className="text-slate-500">Family:</span> {selectedMarketModel.family}</div>
                                    <div><span className="text-slate-500">Parameters:</span> {selectedMarketModel.parameters} ({selectedMarketModel.sizeGiB} GiB)</div>
                                    <div><span className="text-slate-500">Context:</span> {selectedMarketModel.context}</div>
                                </div>
                            </div>
                        ) : (
                            <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-slate-700/80 bg-slate-900/40 p-4 text-center">
                                <p className="text-xs text-slate-400">No model selected from Model Market.</p>
                                <Button type="button" variant="secondary" onClick={() => setMarketPickerOpen(true)} disabled={creating} className="text-xs">
                                    Browse Model Market
                                </Button>
                            </div>
                        )}
                    </div>
                ) : (
                    <>
                        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                            <div>
                                <Label htmlFor="model-cache-repo">HuggingFace repo id</Label>
                                <Input
                                    id="model-cache-repo"
                                    value={repoId}
                                    placeholder="meta-llama/Llama-3-8B"
                                    error={Boolean(repoId) && !repoIdValid}
                                    onChange={(event) => { setRepoId(event.target.value); setSelectedModelTags(null); }}
                                    disabled={creating}
                                />
                            </div>
                            <div>
                                <Label htmlFor="model-cache-revision">Revision</Label>
                                <Input id="model-cache-revision" value={revision} placeholder="main" onChange={(event) => setRevision(event.target.value)} disabled={creating} />
                            </div>
                        </div>

                        {selectedModelTags && (
                            <div className="flex flex-wrap items-center gap-1.5">
                                {selectedModelTags.pipelineTag && (
                                    <span className="rounded-full border border-sky-400/30 bg-sky-400/10 px-2 py-0.5 text-[11px] text-sky-200">
                                        {selectedModelTags.pipelineTag}
                                    </span>
                                )}
                                {selectedModelTags.tags.slice(0, 6).map((tag) => (
                                    <span key={tag} className="rounded-full border border-slate-700 px-2 py-0.5 text-[11px] text-slate-300">
                                        {tag}
                                    </span>
                                ))}
                            </div>
                        )}
                    </>
                )}

                {selectedVolume && (
                    <p className="text-[11px] leading-relaxed text-slate-400">
                        Downloaded with <code className="rounded bg-slate-800 px-1 py-0.5">huggingface-cli download</code> into
                        the standard HuggingFace cache layout, so it is immediately usable by Deployments configured with{' '}
                        <code className="rounded bg-slate-800 px-1 py-0.5">auto-cache</code> model source on this volume.
                    </p>
                )}

                <p className="text-[11px] text-slate-400">
                    Downloads use the HuggingFace token saved with this cluster (set in the cluster wizard);
                    no token selection is needed.
                </p>

                <FormError message={error} />
            </form>

            {pickerOpen && (
                <HuggingFaceModelPickerModal
                    onCancel={() => setPickerOpen(false)}
                    onSelect={({ repoId: pickedRepoId, revision: pickedRevision, pipelineTag, tags }) => {
                        setRepoId(pickedRepoId);
                        setRevision(pickedRevision || 'main');
                        setSelectedModelTags({ pipelineTag, tags: tags || [] });
                        setPickerOpen(false);
                    }}
                />
            )}

            {marketPickerOpen && (
                <ModelMarketPickerModal
                    onCancel={() => setMarketPickerOpen(false)}
                    onSelect={(model) => {
                        handleMarketModelSelect(model.id);
                        setMarketPickerOpen(false);
                    }}
                />
            )}
        </Modal>
    );
}

export default DownloadModelModal;
