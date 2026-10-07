import { useEffect, useState } from 'react';

/** Delay value propagation; callers own normalization and request cancellation. */
export function useDebouncedValue(value, delayMs = 300) {
    const [debouncedValue, setDebouncedValue] = useState(value);
    useEffect(() => {
        const timer = setTimeout(() => setDebouncedValue(value), delayMs);
        return () => clearTimeout(timer);
    }, [value, delayMs]);
    return debouncedValue;
}
