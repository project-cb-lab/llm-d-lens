import React, { useEffect, useRef } from 'react';
import { Modal } from './Modal';
import { Button } from './Button';
import { FormError } from './FormError';

/** Confirmation presentation; callers retain submission and domain decisions. */
export function DeleteConfirmation({
    description, onDecision, title = 'Confirm deletion', subtitle,
    size = onDecision ? 'sm' : 'md', onCancel, pending = false, error,
    formId, onSubmit, confirmLabel = 'Delete', confirmDisabled = false,
    confirmRef, footer, children,
}) {
    const bodyRef = useRef(null);
    const consentOnly = Boolean(onDecision);
    useEffect(() => {
        if (!consentOnly) return undefined;
        const dialog = bodyRef.current?.closest('[role="dialog"]');
        const buttons = dialog?.querySelectorAll('button');
        // Start on the safe action, and keep keyboard navigation inside the dialog.
        buttons?.[buttons.length - 2]?.focus();
        const trapFocus = (event) => {
            if (event.key !== 'Tab' || !buttons?.length) return;
            const first = buttons[0];
            const last = buttons[buttons.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault(); last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault(); first.focus();
            }
        };
        dialog?.addEventListener('keydown', trapFocus);
        return () => dialog?.removeEventListener('keydown', trapFocus);
    }, [consentOnly]);
    const cancel = pending ? undefined : (onCancel || (() => onDecision?.(false)));
    const content = <>
        {description && <p data-delete-confirmation className="break-words text-sm leading-6 text-theme-muted">{description}</p>}
        {children}
        <FormError message={error} />
    </>;
    return <Modal isOpen title={title} subtitle={subtitle} size={size} onClose={cancel}
        closeOnBackdrop={!pending} closeOnEscape={!pending} footer={footer ?? <>
            <Button variant="secondary" onClick={cancel} disabled={pending}>Cancel</Button>
            <Button ref={confirmRef} type={formId ? 'submit' : 'button'} form={formId}
                variant="danger" isLoading={pending} disabled={pending || confirmDisabled}
                onClick={formId ? undefined : () => onDecision?.(true)}>{confirmLabel}</Button>
        </>}>
        {formId
            ? <form ref={bodyRef} id={formId} onSubmit={onSubmit} className="flex flex-col gap-4">{content}</form>
            : <div ref={bodyRef} className="flex flex-col gap-4">{content}</div>}
    </Modal>;
}
