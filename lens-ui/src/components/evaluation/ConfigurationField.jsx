import { Children, cloneElement, isValidElement, useContext, useId, useState } from 'react';

import { ConfigurationValidationContext } from './configurationFieldState';

export default function ConfigurationField({ field, children, className = '', suggestion }) {
    const { errors, submitted } = useContext(ConfigurationValidationContext);
    const [touched, setTouched] = useState(false);
    const id = useId();
    const messages = submitted || touched ? errors.filter(error => error.field === field) : [];
    return <label tabIndex={-1} data-configuration-field={field} className={`block min-w-0 ${className}`} onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setTouched(true);
    }}>
        {Children.map(children, child => isValidElement(child) && ['input', 'select', 'textarea'].includes(child.type) ? cloneElement(child, {
            'aria-invalid': messages.length ? true : undefined,
            'aria-describedby': messages.length ? [child.props['aria-describedby'], id].filter(Boolean).join(' ') : child.props['aria-describedby'],
            className: `${child.props.className || ''}${messages.length ? ' !border-rose-400 focus:!border-rose-400' : ''}`,
        }) : child)}
        {messages.length > 0 && <span id={id} className="mt-1 block text-xs leading-5 text-rose-300">{messages.map(error => error.message).join(' ')}</span>}
        {suggestion}
    </label>;
}

export function FieldHelp({ children }) {
    return <span className="mt-2 block basis-full text-xs leading-5 text-slate-400">{children}</span>;
}
