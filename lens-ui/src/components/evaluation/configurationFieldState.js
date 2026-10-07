import { createContext } from 'react';
export const ConfigurationValidationContext = createContext({ errors: [], submitted: false });
const lastErrorTargets = new WeakMap();

/** Open every enclosing disclosure before scrolling; field names are not CSS selectors. */
export function focusConfigurationError(root, errors, next = false) {
    if (!root) return;
    const fields = [...root.querySelectorAll('[data-configuration-field]')].filter(node => errors.some(error => error.field === node.dataset.configurationField));
    const active = fields.findIndex(node => node.contains(root.ownerDocument.activeElement));
    const current = active >= 0 ? active : fields.indexOf(lastErrorTargets.get(root));
    const target = fields[next ? (current + 1) % fields.length : 0];
    if (!target) return;
    lastErrorTargets.set(root, target);
    for (let node = target.parentElement; node && node !== root; node = node.parentElement) {
        if (node.tagName === 'DETAILS') node.open = true;
    }
    const control = target.querySelector('input:not(:disabled), select:not(:disabled), textarea:not(:disabled), button:not(:disabled)') || target;
    control.focus({ preventScroll: true });
    target.scrollIntoView({ behavior: root.ownerDocument.defaultView?.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'center' });
}
