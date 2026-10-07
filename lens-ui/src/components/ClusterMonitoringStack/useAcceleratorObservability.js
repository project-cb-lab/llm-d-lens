// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import { useCallback, useEffect, useRef, useState } from 'react';
import { getAcceleratorStatus } from './acceleratorObservabilityBackend';

const RETRY_DELAYS = [5000, 10000, 20000, 30000];

export function useAcceleratorObservability(accelerator, namespace, accessMode, clusterId) {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(true);
    const [stale, setStale] = useState(false);
    const [lastUpdated, setLastUpdated] = useState(null);
    const controllerRef = useRef(null);
    const failureCountRef = useRef(0);
    const hasDataRef = useRef(false);
    const hasCluster = Boolean(clusterId && accelerator && namespace);

    const refresh = useCallback(async () => {
        if (!hasCluster) return null;
        controllerRef.current?.abort();
        const controller = new AbortController();
        controllerRef.current = controller;
        if (!hasDataRef.current) setLoading(true);

        try {
            const snapshot = await getAcceleratorStatus(accelerator, {
                namespace,
                accessMode,
                signal: controller.signal,
                clusterId,
            });
            if (controller.signal.aborted) return null;
            failureCountRef.current = 0;
            hasDataRef.current = true;
            setData(snapshot);
            setError(null);
            setStale(Boolean(snapshot?.stale));
            setLastUpdated(new Date());
            return snapshot;
        } catch (requestError) {
            if (requestError.name === 'AbortError') return null;
            failureCountRef.current += 1;
            setError(requestError);
            setStale(true);
            return null;
        } finally {
            if (!controller.signal.aborted) setLoading(false);
        }
    }, [accelerator, namespace, accessMode, clusterId, hasCluster]);

    useEffect(() => {
        setData(null);
        setError(null);
        setStale(false);
        setLastUpdated(null);
        failureCountRef.current = 0;
        hasDataRef.current = false;
        setLoading(hasCluster);
    }, [accelerator, namespace, accessMode, clusterId, hasCluster]);

    useEffect(() => {
        if (!hasCluster) return undefined;

        let cancelled = false;
        let timer = null;

        const schedule = () => {
            if (cancelled || document.hidden) return;
            const failures = failureCountRef.current;
            const delay = failures
                ? RETRY_DELAYS[Math.min(failures - 1, RETRY_DELAYS.length - 1)]
                : RETRY_DELAYS[0];
            timer = window.setTimeout(run, delay);
        };

        const run = async () => {
            await refresh();
            schedule();
        };

        const onVisibilityChange = () => {
            window.clearTimeout(timer);
            if (!document.hidden) run();
            else controllerRef.current?.abort();
        };

        if (!document.hidden) run();
        document.addEventListener('visibilitychange', onVisibilityChange);
        return () => {
            cancelled = true;
            window.clearTimeout(timer);
            controllerRef.current?.abort();
            document.removeEventListener('visibilitychange', onVisibilityChange);
        };
    }, [refresh, hasCluster]);

    return { data, error, loading, stale, lastUpdated, refresh };
}
