import { useCallback, useEffect, useRef, useState } from 'react';
import { createLatestResourceRequest } from '../utils/resourceLoader';
import { errorMessage } from '../utils/errorMessage';

/** The caller owns query/filter semantics; this hook owns request and state lifetime. */
export function useResourceList(loadResource, { onLoaded, errorFallback = 'Failed to load resources', retainOnError = false } = {}) {
    const [items, setItems] = useState([]);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState('');
    const [request] = useState(createLatestResourceRequest);
    const loadedOnce = useRef(false);
    const load = useCallback(async ({ quiet = false } = {}) => {
        if (quiet && request.pending) return;
        if (!quiet && loadedOnce.current) setRefreshing(true);
        await request.run(signal => loadResource({ signal }), {
            onSuccess: results => {
                setItems(results);
                setError('');
                onLoaded?.(results);
            },
            onError: failure => {
                setError(errorMessage(failure, errorFallback));
                if (!retainOnError) setItems([]);
            },
            onSettled: () => {
                loadedOnce.current = true;
                setLoading(false);
                setRefreshing(false);
            },
        });
    }, [request, loadResource, onLoaded, errorFallback, retainOnError]);

    useEffect(() => {
        let active = true;
        Promise.resolve().then(() => { if (active) load(); });
        return () => { active = false; request.cancel(); };
    }, [load, request]);

    return { items, setItems, loading, refreshing, error, setError, load };
}
