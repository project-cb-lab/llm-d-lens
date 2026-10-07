import { useEffect } from 'react';
const ACTIVE_OPERATION_STATUSES = new Set(['queued', 'running']);

export function useMonitoringOperation({ operationId, operationStatus, completedOperationId, namespace, loadOperation, refreshStatus, setOperation, setOperationError, setCompletedOperationId, setInstallSuccess }) {
    useEffect(() => {
        if (!operationId || !ACTIVE_OPERATION_STATUSES.has(operationStatus || 'running')) return undefined;
        let cancelled = false;
        let timer = null;
        let controller = null;

        const poll = async () => {
            if (cancelled || document.hidden) return;
            controller?.abort();
            controller = new AbortController();
            try {
                const nextOperation = await loadOperation(operationId, { signal: controller.signal });
                if (cancelled) return;
                setOperationError(null);
                if (nextOperation.status === 'succeeded' && completedOperationId !== nextOperation.operation_id) {
                    setCompletedOperationId(nextOperation.operation_id);
                    setInstallSuccess({
                        operationId: nextOperation.operation_id,
                        namespace: nextOperation.namespace || namespace,
                    });
                    setOperation(null);
                    refreshStatus();
                    return;
                }
                setOperation(nextOperation);
                if (nextOperation.status === 'failed') return;
            } catch (error) {
                if (error.name !== 'AbortError') setOperationError(error);
            }
            if (!cancelled) timer = window.setTimeout(poll, 2000);
        };

        const onVisibilityChange = () => {
            window.clearTimeout(timer);
            if (document.hidden) controller?.abort();
            else poll();
        };

        poll();
        document.addEventListener('visibilitychange', onVisibilityChange);
        return () => {
            cancelled = true;
            window.clearTimeout(timer);
            controller?.abort();
            document.removeEventListener('visibilitychange', onVisibilityChange);
        };
    }, [completedOperationId, namespace, operationId, operationStatus, refreshStatus, loadOperation, setOperation, setOperationError, setCompletedOperationId, setInstallSuccess]);

}
