import { useCallback, useEffect, useRef, useState } from 'react';

function idempotencyKey() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function useMonitoringInstall({ request, setRequest, preflightRequest, installRequest, clusterId, onOperationStarted, onClose }) {
    const [preflight, setPreflight] = useState(null);
    const [error, setError] = useState(null);
    const [checking, setChecking] = useState(false);
    const [installing, setInstalling] = useState(false);
    const keyRef = useRef(null);
    const controllerRef = useRef(null);

    useEffect(() => () => controllerRef.current?.abort(), []);

    const updateRequest = (change) => {
        controllerRef.current?.abort();
        setRequest((current) => ({ ...current, ...change }));
        setPreflight(null);
        setError(null);
        keyRef.current = null;
    };

    const runPreflight = async () => {
        controllerRef.current?.abort();
        const controller = new AbortController();
        controllerRef.current = controller;
        setChecking(true);
        setError(null);
        try {
            const result = await preflightRequest(request, { signal: controller.signal, clusterId });
            setPreflight(result);
            keyRef.current = result?.allowed ? idempotencyKey() : null;
        } catch (preflightError) {
            if (preflightError.name !== 'AbortError') setError(preflightError);
        } finally {
            if (!controller.signal.aborted) setChecking(false);
        }
    };

    const startInstall = async () => {
        if (!preflight?.allowed || !keyRef.current) return;
        setInstalling(true);
        setError(null);
        try {
            const operation = await installRequest(request, keyRef.current, clusterId);
            onOperationStarted(operation);
            onClose();
        } catch (installError) {
            setError(installError);
            setPreflight(null);
            keyRef.current = null;
        } finally {
            setInstalling(false);
        }
    };

    const reset = useCallback(() => {
        setPreflight(null);
        setError(null);
        keyRef.current = null;
    }, []);
    return { preflight, error, checking, installing, updateRequest, runPreflight, startInstall, reset };
}
