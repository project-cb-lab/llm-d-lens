import React from 'react';

export function FormError({ message }) {
    if (!message) return null;
    return <p role="alert" className="rounded-lg border border-rose-300 bg-rose-50 px-3 py-2 text-xs text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-200">{message}</p>;
}
