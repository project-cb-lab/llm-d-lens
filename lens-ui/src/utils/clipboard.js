/** Copy text in secure contexts and legacy HTTP deployments. Reject on failure. */
export async function copyText(value) {
    if (globalThis.navigator?.clipboard?.writeText) {
        try {
            await navigator.clipboard.writeText(value);
            return;
        } catch {
            // Some browsers expose the API but deny access; try the HTTP fallback.
        }
    }
    const previousFocus = document.activeElement;
    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    try {
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        if (!document.execCommand('copy')) throw new Error('Copy failed');
    } finally {
        textarea.remove();
        previousFocus?.focus?.();
    }
}
