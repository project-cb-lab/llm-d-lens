import { useCallback, useEffect, useRef, useState } from 'react';
import { copyText } from '../utils/clipboard';

/** Success feedback for the latest copy action; failure returns false. */
export function useClipboard(durationMs = 1500) {
    const [copiedValue, setCopiedValue] = useState('');
    const timer = useRef(null);
    const request = useRef(0);
    useEffect(() => () => {
        request.current += 1;
        clearTimeout(timer.current);
    }, []);
    const copy = useCallback(async (value) => {
        if (!value) return false;
        const id = ++request.current;
        clearTimeout(timer.current);
        setCopiedValue('');
        try {
            await copyText(value);
            if (id === request.current) {
                setCopiedValue(value);
                timer.current = setTimeout(() => setCopiedValue(''), durationMs);
            }
            return true;
        } catch {
            return false;
        }
    }, [durationMs]);
    return { copied: Boolean(copiedValue), copiedValue, copy };
}
