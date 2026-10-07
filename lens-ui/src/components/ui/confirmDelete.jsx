import React from 'react';
import { createRoot } from 'react-dom/client';
import { DeleteConfirmation } from './DeleteConfirmation';

let confirmationOpen = false;

/** Await explicit consent before an existing deletion handler runs. */
export function confirmDelete(description) {
    // Ignore repeated triggers instead of opening stacked confirmation dialogs.
    if (confirmationOpen) return Promise.resolve(false);
    confirmationOpen = true;
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    return new Promise((resolve) => {
        let settled = false;
        const onDecision = (confirmed) => {
            if (settled) return;
            settled = true;
            root.unmount();
            container.remove();
            document.body.style.overflow = previousOverflow;
            confirmationOpen = false;
            if (previousFocus?.isConnected) previousFocus.focus();
            resolve(confirmed);
        };
        root.render(<DeleteConfirmation description={description} onDecision={onDecision} />);
    });
}
