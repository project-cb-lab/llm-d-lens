// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import React, { useEffect, useRef, useState } from 'react';
import { Clock3, Terminal } from 'lucide-react';
import { Button, Panel, StatusChip } from '../ui';

function duration(startedAt, finishedAt, now) {
    if (!startedAt) return '—';
    const end = finishedAt ? new Date(finishedAt).getTime() : now;
    const seconds = Math.max(0, Math.floor((end - new Date(startedAt).getTime()) / 1000));
    const minutes = Math.floor(seconds / 60);
    return minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}

export function ClusterStackOperationProgress({ operation, error, onRetryPreflight }) {
    const [now, setNow] = useState(0);
    const logsRef = useRef(null);
    const logs = operation?.logs || [];
    const latestLogMessage = logs.at(-1)?.message;

    useEffect(() => {
        if (!operation || ['succeeded', 'failed'].includes(operation.status)) return undefined;
        const timer = window.setInterval(() => setNow(Date.now()), 1000);
        return () => window.clearInterval(timer);
    }, [operation]);

    useEffect(() => {
        const container = logsRef.current;
        if (!container) return;
        container.scrollTop = container.scrollHeight;
    }, [logs.length, latestLogMessage]);

    if (!operation && !error) return null;
    const operationError = operation?.error;
    const chipStatus = operation?.status === 'succeeded'
        ? 'verified'
        : operation?.status === 'queued'
            ? 'pending'
            : operation?.status;

    return (
        <Panel
            title={<span className="text-[11px] font-extrabold uppercase tracking-widest text-violet-400/90">Installation operation</span>}
            className="overflow-hidden border-slate-800/80 bg-gradient-to-br from-slate-900 to-slate-950 shadow-xl"
            actions={operation?.status === 'failed' && onRetryPreflight
                ? <Button variant="outline" onClick={onRetryPreflight}>Retry preflight</Button>
                : null}
        >
            {operation && (
                <>
                    <div className="grid gap-3 sm:grid-cols-3">
                        <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3">
                            <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Status</div>
                            <div className="mt-1">
                                <StatusChip
                                    status={chipStatus}
                                    label={operation.status}
                                    pulse={['queued', 'running'].includes(operation.status)}
                                />
                            </div>
                        </div>
                        <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3">
                            <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Phase</div>
                            <div className="mt-1 text-sm font-semibold text-slate-200">{String(operation.phase || 'queued').replace(/_/g, ' ')}</div>
                        </div>
                        <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3">
                            <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Duration</div>
                            <div className="mt-1 inline-flex items-center gap-1.5 text-sm font-semibold text-slate-200">
                                <Clock3 className="h-4 w-4 text-theme-muted" />
                                {duration(operation.started_at, operation.finished_at, now)}
                            </div>
                        </div>
                    </div>
                    {(operationError || error) && (
                        <div className="mt-4 rounded-lg border border-red-300 dark:border-red-500/40 bg-red-50 dark:bg-red-500/10 p-3 text-xs text-red-700 dark:text-red-300">
                            <strong>{operationError?.code || error?.code || 'OPERATION_FAILED'}:</strong>{' '}
                            {operationError?.message || error?.message || String(operationError)}
                        </div>
                    )}
                    <div className="mt-5">
                        <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-theme-muted">
                            <Terminal className="h-4 w-4" /> Recent logs
                        </div>
                        <div
                            ref={logsRef}
                            className="max-h-72 overflow-y-auto scroll-smooth rounded-lg border border-slate-800 bg-black/40 p-3 font-mono text-xs shadow-inner"
                        >
                            {logs.length ? logs.map((log) => (
                                <div key={log.sequence ?? `${log.timestamp}-${log.message}`} className="mb-1 grid grid-cols-[auto_auto_1fr] gap-2 text-theme-muted">
                                    <span>{log.timestamp ? new Date(log.timestamp).toLocaleTimeString() : '—'}</span>
                                    <span className="uppercase">{log.level || 'info'}</span>
                                    <span className="text-theme-text whitespace-pre-wrap break-words">{log.message}</span>
                                </div>
                            )) : <span className="text-theme-muted">Waiting for operation logs…</span>}
                        </div>
                    </div>
                </>
            )}
            {!operation && error && (
                <div className="text-sm text-red-700 dark:text-red-300">{error.message}</div>
            )}
        </Panel>
    );
}
