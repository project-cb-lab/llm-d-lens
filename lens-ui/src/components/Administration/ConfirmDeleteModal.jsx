import { FormError } from '../ui/FormError';
import { useSubmission } from '../../hooks/useSubmission';
import { useEffect, useRef } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';

const FORM_ID = 'admin-confirm-delete-form';

/** Reusable destructive confirmation dialog for Administration pages. */
export function ConfirmDeleteModal({ title, subtitle, warning, confirmLabel = 'Delete', onCancel, onDelete }) {
    const { pending, error, run } = useSubmission('Failed to delete', { keepPendingOnSuccess: true });
    const confirmRef = useRef(null);

    useEffect(() => {
        confirmRef.current?.focus();
    }, []);

    const submit = async (event) => {
        event.preventDefault();
        if (pending) return;
        await run(() => onDelete());
    };

    return (
        <Modal
            isOpen
            onClose={pending ? undefined : onCancel}
            title={title}
            subtitle={subtitle}
            closeOnBackdrop={!pending}
            closeOnEscape={!pending}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={pending}>
                        Cancel
                    </Button>
                    <Button ref={confirmRef} type="submit" form={FORM_ID} variant="danger" isLoading={pending}>
                        {pending ? 'Deleting' : confirmLabel}
                    </Button>
                </>
            }
        >
            <form id={FORM_ID} onSubmit={submit} className="flex flex-col gap-4">
                {warning && (
                    <div className="flex gap-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2.5">
                        <AlertTriangle size={16} className="mt-0.5 shrink-0 text-rose-300" aria-hidden="true" />
                        <p className="text-xs leading-relaxed text-rose-100">{warning}</p>
                    </div>
                )}
                <FormError message={error} />
            </form>
        </Modal>
    );
}

export default ConfirmDeleteModal;
