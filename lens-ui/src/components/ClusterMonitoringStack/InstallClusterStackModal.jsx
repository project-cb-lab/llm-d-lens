// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import { checkPassed } from './preflightPresentation';
import { useMonitoringInstall } from './useMonitoringInstall';
import React, { useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, CircleX } from 'lucide-react';
import { Badge, Button, Checkbox, Input, Label, Modal, ToggleGroup } from '../ui';
import { installClusterStack, preflightClusterStackInstall } from './clusterMonitoringStackBackend';

export function InstallClusterStackModal({ isOpen, namespace, clusterId, reinstall = false, onClose, onOperationStarted }) {
    const [request, setRequest] = useState({ namespace, mode: 'central', enable_tls: false, reinstall });
    const { preflight, error, checking, installing, updateRequest, runPreflight, startInstall, reset } = useMonitoringInstall({
        request, setRequest, preflightRequest: preflightClusterStackInstall, installRequest: installClusterStack,
        clusterId, onOperationStarted, onClose,
    });
    const [confirmed, setConfirmed] = useState(false);
    useEffect(() => {
        if (!isOpen) return;
        setRequest({ namespace, mode: 'central', enable_tls: false, reinstall });
        reset();
        setConfirmed(false);
    }, [isOpen, namespace, reinstall, reset]);

    const footer = (
        <>
            <Button variant="ghost" onClick={onClose} disabled={installing}>Cancel</Button>
            {!preflight?.allowed ? (
                <Button onClick={runPreflight} isLoading={checking} disabled={!request.namespace.trim()}>
                    Run preflight
                </Button>
            ) : (
                <Button onClick={startInstall} isLoading={installing} disabled={reinstall && !confirmed}>
                    {reinstall ? 'Reinstall stack' : 'Install stack'}
                </Button>
            )}
        </>
    );

    return (
        <Modal
            isOpen={isOpen}
            onClose={installing ? undefined : onClose}
            title={reinstall ? 'Reinstall observability stack' : 'Install observability stack'}
            subtitle={reinstall
                ? 'Reinstalling removes existing metrics. Confirm to continue.'
                : 'Preflight must pass before installation can begin.'}
            size="lg"
            footer={footer}
            closeOnBackdrop={!installing}
            closeOnEscape={!installing}
        >
            <div className="space-y-5">
                <div>
                    <Label htmlFor="cluster-stack-namespace">Namespace</Label>
                    <Input
                        id="cluster-stack-namespace"
                        value={request.namespace}
                        onChange={(event) => updateRequest({ namespace: event.target.value })}
                        disabled={checking || installing}
                    />
                </div>
                <div>
                    <Label>Installation mode</Label>
                    <ToggleGroup
                        options={[
                            { value: 'central', label: 'Central (recommended)' },
                            { value: 'individual', label: 'Individual', disabled: true },
                        ]}
                        value={request.mode}
                        onChange={(mode) => updateRequest({ mode })}
                        fullWidth
                    />
                    <p className="mt-2 text-xs text-theme-muted">
                        Individual mode is not yet available.
                    </p>
                </div>
                <Checkbox
                    checked={request.enable_tls}
                    onChange={(event) => updateRequest({ enable_tls: event.target.checked })}
                    label="Enable TLS"
                    disabled
                />

                {reinstall && (
                    <div className="rounded-lg border border-red-300 dark:border-red-500/40 bg-red-50 dark:bg-red-500/10 p-3">
                        <div className="flex gap-2 text-xs text-red-700 dark:text-red-300">
                            <AlertTriangle className="h-4 w-4 shrink-0" />
                            <span>
                                <strong>Reinstalling deletes stored metrics.</strong> The existing
                                Prometheus and Grafana release and its namespace will be removed
                                before reinstallation, permanently deleting all collected metrics.
                            </span>
                        </div>
                        <div className="mt-3">
                            <Checkbox
                                checked={confirmed}
                                onChange={(event) => setConfirmed(event.target.checked)}
                                label="I understand existing metrics data will be permanently deleted"
                            />
                        </div>
                    </div>
                )}

                {error && (
                    <div className="rounded-lg border border-red-300 dark:border-red-500/40 bg-red-50 dark:bg-red-500/10 p-3 text-xs text-red-700 dark:text-red-300">
                        <strong>{error.code || 'REQUEST_FAILED'}:</strong> {error.message}
                    </div>
                )}

                {preflight && (
                    <div className="space-y-3">
                        <div className="flex items-center gap-2">
                            {preflight.allowed
                                ? <CheckCircle2 className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                                : <CircleX className="h-4 w-4 text-red-600 dark:text-red-400" />}
                            <span className="text-sm font-semibold text-theme-text">
                                Preflight {preflight.allowed ? 'passed' : 'did not pass'}
                            </span>
                        </div>
                        <div className="space-y-2">
                            {(preflight.checks || []).map((check, index) => (
                                <div key={check.name || check.code || index} className="flex items-start justify-between gap-3 rounded-lg border border-theme-border p-3">
                                    <div>
                                        <div className="text-xs font-semibold text-theme-text">{check.name || check.code || `Check ${index + 1}`}</div>
                                        {check.message && <div className="mt-1 text-xs text-theme-muted">{check.message}</div>}
                                    </div>
                                    <Badge tone={checkPassed(check) ? 'success' : 'danger'}>
                                        {checkPassed(check) ? 'Passed' : check.status || 'Failed'}
                                    </Badge>
                                </div>
                            ))}
                        </div>
                        {(preflight.warnings || []).map((warning, index) => (
                            <div key={warning.code || index} className="flex gap-2 rounded-lg border border-amber-300 dark:border-amber-500/40 bg-amber-50 dark:bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-300">
                                <AlertTriangle className="h-4 w-4 shrink-0" />
                                <span>{warning.message || warning}</span>
                            </div>
                        ))}
                        {preflight.command_preview && (
                            <div>
                                <Label>Command preview</Label>
                                <pre className="overflow-x-auto rounded-lg border border-theme-border bg-slate-100 dark:bg-slate-950 p-3 text-xs text-theme-muted">
                                    {Array.isArray(preflight.command_preview)
                                        ? preflight.command_preview.join(' ')
                                        : preflight.command_preview}
                                </pre>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </Modal>
    );
}
