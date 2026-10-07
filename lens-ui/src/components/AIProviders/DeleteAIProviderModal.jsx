import { useSubmission } from '../../hooks/useSubmission';
import { useEffect, useRef } from 'react';
import { AlertTriangle } from 'lucide-react';
import { DeleteConfirmation } from '../ui/DeleteConfirmation';
import { providerTitle } from './aiProvidersPresentation';

export function DeleteAIProviderModal({ provider, onCancel, onDelete }) {
    const { pending: deleting, error, run } = useSubmission('Failed to delete AI provider', { keepPendingOnSuccess: true });
    const confirmRef = useRef(null);

    useEffect(() => {
        confirmRef.current?.focus();
    }, []);

    if (!provider) return null;

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
            title="Delete AI provider"
            subtitle={providerTitle(provider)}
            formId="delete-ai-provider-form"
            onSubmit={submit}
            confirmLabel={deleting ? 'Deleting' : 'Delete provider'}
            confirmRef={confirmRef}
        >
            <div className="flex gap-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2.5">
                <AlertTriangle size={16} className="mt-0.5 shrink-0 text-rose-300" aria-hidden="true" />
                <p className="text-xs leading-relaxed text-rose-100">
                    Any Agentic component currently configured to use this provider will fall back
                    to the default planning behavior the next time it runs.
                </p>
            </div>
        </DeleteConfirmation>
    );
}

export default DeleteAIProviderModal;
