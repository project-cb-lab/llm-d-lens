import { useEffect, useState } from 'react';

/** Transient message state, with the same setter contract as useState. */
export function useNotice(durationMs = 4000) {
    const [notice, setNotice] = useState('');
    useEffect(() => {
        if (!notice) return undefined;
        const timer = setTimeout(() => setNotice(''), durationMs);
        return () => clearTimeout(timer);
    }, [notice, durationMs]);
    return [notice, setNotice];
}
