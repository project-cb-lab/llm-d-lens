import { useSubmission } from '../../hooks/useSubmission';
import { useEffect, useRef, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { DeleteConfirmation } from '../ui/DeleteConfirmation';
import { Button } from '../ui/Button';
import { volumeLocation, volumeTitle } from './storagePresentation';

// Two-step flow: (1) confirm deleting the volume also removes its Model
// Cache entries (skipped if there are none), then (2) choose whether the
// actual model files on NFS/hostPath should be kept or really deleted.
export function DeleteStorageVolumeModal({ volume, onCancel, onDelete }) {
    const [step, setStep] = useState('confirm');
    const { pending: deleting, error, setError, run } = useSubmission('Failed to delete storage volume', { keepPendingOnSuccess: true });
    const confirmRef = useRef(null);
    const lastVolumeIdRef = useRef(volume?.id);

    const modelCacheCount = volume?.modelCacheCount || 0;
    const hasModelCaches = modelCacheCount > 0;

    // Reset to step 1 whenever a different volume is targeted (e.g. the
    // modal is reused for a second delete after the first closes). Derived
    // during render rather than an effect so it can't cause an extra render.
    if (volume?.id !== lastVolumeIdRef.current) {
        lastVolumeIdRef.current = volume?.id;
        if (step !== 'confirm') setStep('confirm');
        if (error) setError('');
    }

    useEffect(() => {
        confirmRef.current?.focus();
    }, [step]);

    if (!volume) return null;

    const runDelete = async (keepModelFiles) => {
        if (deleting) return;
        await run(() => onDelete(keepModelFiles));
    };

    const submitConfirm = (event) => {
        event.preventDefault();
        if (hasModelCaches) {
            setStep('keep-files');
            return;
        }
        runDelete(false);
    };

    if (step === 'keep-files') {
        return (
            <DeleteConfirmation
                onCancel={onCancel}
                pending={deleting}
                error={error}
                title="Delete storage volume"
                subtitle={volumeTitle(volume)}
                footer={
                    <>
                        <Button variant="secondary" onClick={() => setStep('confirm')} disabled={deleting}>Back</Button>
                        <Button variant="secondary" onClick={() => runDelete(true)} disabled={deleting} isLoading={deleting}>
                            Keep model files
                        </Button>
                        <Button ref={confirmRef} variant="danger" onClick={() => runDelete(false)} disabled={deleting} isLoading={deleting}>
                            Delete model files
                        </Button>
                    </>
                }
            >
                <div className="flex flex-col gap-4">
                    <p className="text-sm text-slate-300">
                        Do you want to keep the model files already downloaded onto this storage, or delete them too?
                    </p>
                    <ul className="flex flex-col gap-2 text-xs text-slate-300">
                        <li className="rounded-lg border border-slate-700/60 bg-slate-800/60 px-3 py-2">
                            <strong className="text-slate-100">Keep model files</strong> — only removes the {modelCacheCount}{' '}
                            model cache record{modelCacheCount === 1 ? '' : 's'} and the storage volume. Files already on
                            NFS/hostPath are left untouched.
                        </li>
                        <li className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-rose-100">
                            <strong>Delete model files</strong> — actually removes the downloaded model files from
                            NFS/hostPath for each model cache entry before deleting the storage volume.
                        </li>
                    </ul>
                </div>
            </DeleteConfirmation>
        );
    }

    return (
        <DeleteConfirmation
            onCancel={onCancel}
            pending={deleting}
            error={error}
            title="Delete storage volume"
            subtitle={volumeTitle(volume)}
            formId="delete-storage-volume-form"
            onSubmit={submitConfirm}
            confirmLabel={hasModelCaches ? 'Continue' : (deleting ? 'Deleting' : 'Delete storage volume')}
            confirmRef={confirmRef}
        >
            {hasModelCaches && (
                <div className="flex gap-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2.5">
                    <AlertTriangle size={16} className="mt-0.5 shrink-0 text-rose-300" aria-hidden="true" />
                    <p className="text-xs leading-relaxed text-rose-100">
                        This storage volume has <strong>{modelCacheCount}</strong> model cache
                        {modelCacheCount === 1 ? ' entry' : ' entries'}. Deleting the volume will also remove{' '}
                        {modelCacheCount === 1 ? 'it' : 'all of them'}. You'll choose next whether to keep the
                        downloaded model files.
                    </p>
                </div>
            )}

            <div className="flex gap-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2.5">
                <AlertTriangle size={16} className="mt-0.5 shrink-0 text-rose-300" aria-hidden="true" />
                <p className="text-xs leading-relaxed text-rose-100">
                    This deletes the PersistentVolumeClaim{volume.pvcName ? ` "${volume.pvcName}"` : ''}
                    {volume.pvName ? ` and PersistentVolume "${volume.pvName}"` : ''} from Kubernetes.
                    Data on the underlying disk/NFS export is <strong>not</strong> deleted automatically
                    (reclaim policy: Retain) — clean it up manually if needed.
                </p>
            </div>

            <dl className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
                <div>
                    <dt className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Type</dt>
                    <dd className="mt-0.5 text-slate-300">{volume.kind}</dd>
                </div>
                <div className="sm:col-span-2">
                    <dt className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Location</dt>
                    <dd className="mt-0.5 break-all font-mono text-[11px] text-slate-300">{volumeLocation(volume)}</dd>
                </div>
            </dl>
        </DeleteConfirmation>
    );
}

export default DeleteStorageVolumeModal;
