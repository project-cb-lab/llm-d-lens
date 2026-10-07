import { useSubmission } from '../../hooks/useSubmission';
import { useEffect, useRef } from 'react';
import { AlertTriangle } from 'lucide-react';
import { DeleteConfirmation } from '../ui/DeleteConfirmation';
import { sourceDisplayName } from './modelCachePresentation';

export function DeleteModelCacheModal({ entry, onCancel, onDelete }) {
    const { pending: deleting, error, run } = useSubmission('Failed to delete cached model', { keepPendingOnSuccess: true });
    const confirmRef = useRef(null);

    useEffect(() => {
        confirmRef.current?.focus();
    }, []);

    if (!entry) return null;

    const submit = async (event) => {
        event.preventDefault();
        if (deleting) return;
        await run(() => onDelete());
    };

    return (
        <DeleteConfirmation
            onCancel={onCancel}
            pending={deleting}
            error={error}
            title="Delete cached model"
            subtitle={sourceDisplayName(entry)}
            formId="delete-model-cache-form"
            onSubmit={submit}
            confirmLabel={deleting ? 'Deleting' : 'Delete cached model'}
            confirmRef={confirmRef}
        >
            <div className="flex gap-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2.5">
                <AlertTriangle size={16} className="mt-0.5 shrink-0 text-rose-300" aria-hidden="true" />
                <p className="text-xs leading-relaxed text-rose-100">
                    This removes only this model&apos;s files (<code className="rounded bg-rose-950/40 px-1 py-0.5">{entry.cachePath}</code>) from the
                    storage volume. Other cached models on the same volume are not affected, and the storage
                    volume itself is not deleted.
                </p>
            </div>
        </DeleteConfirmation>
    );
}

export default DeleteModelCacheModal;
