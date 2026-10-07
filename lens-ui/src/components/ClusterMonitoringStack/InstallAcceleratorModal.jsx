// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import { checkPassed, checkTone, checkLabel } from './preflightPresentation';
import { useMonitoringInstall } from './useMonitoringInstall';
import React, { useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, CircleX } from 'lucide-react';
import { Badge, Button, Input, Label, Modal, ToggleGroup } from '../ui';
import { installAccelerator, preflightAcceleratorInstall } from './acceleratorObservabilityBackend';
import { accessModeLabel } from './acceleratorLabels';

function initialAccessModeFor(status, supportedAccessModes = []) {
    const modes = status?.access_modes || [];
    const preferred = modes.find((entry) => entry.available) || modes[0];
    return preferred?.mode || supportedAccessModes[0] || 'dra';
}

export function InstallAcceleratorModal({ isOpen, accelerator, defaultNamespace, status, supportedAccessModes = [], initialAccessMode, clusterId, onClose, onOperationStarted }) {
    const [request, setRequest] = useState({
        accelerator,
        access_mode: initialAccessModeFor(status, supportedAccessModes),
        namespace: defaultNamespace || '',
    });
    const { preflight, error, checking, installing, updateRequest, runPreflight, startInstall, reset } = useMonitoringInstall({
        request, setRequest, preflightRequest: preflightAcceleratorInstall, installRequest: installAccelerator,
        clusterId, onOperationStarted, onClose,
    });
    const prevOpenRef = useRef(false);
    useEffect(() => {
        const wasOpen = prevOpenRef.current;
        prevOpenRef.current = isOpen;
        if (!isOpen || wasOpen) return;
        setRequest({
            accelerator,
            access_mode: initialAccessMode || initialAccessModeFor(status, supportedAccessModes),
            namespace: defaultNamespace || '',
        });
        reset();
    }, [isOpen, accelerator, defaultNamespace, status, supportedAccessModes, initialAccessMode, reset]);

    // Modes come from the selected accelerator: the status payload reports
    // availability per detected mode, and the capability lists the supported
    // set before any cluster probe has run.
    const accessModeEntries = status?.access_modes?.length
        ? status.access_modes
        : supportedAccessModes.map((mode) => ({ mode, available: true, detected: true, message: null }));
    const accessModes = accessModeEntries.map((entry) => ({
        value: entry.mode,
        label: accessModeLabel(entry.mode),
        disabled: !entry.available,
        message: entry.available ? null : entry.message,
    }));
    const selectedUnavailable = accessModes.find((mode) => mode.value === request.access_mode)?.disabled;
    const unavailableMessages = accessModes
        .filter((mode) => mode.value === request.access_mode && mode.disabled && mode.message)
        .map((mode) => mode.message);

    const footer = (
        <>
            <Button variant="ghost" onClick={onClose} disabled={installing}>Cancel</Button>
            {!preflight?.allowed ? (
                <Button
                    onClick={runPreflight}
                    isLoading={checking}
                    disabled={!request.namespace.trim() || selectedUnavailable}
                >
                    Run preflight
                </Button>
            ) : (
                <Button onClick={startInstall} isLoading={installing}>Install</Button>
            )}
        </>
    );

    return (
        <Modal
            isOpen={isOpen}
            onClose={installing ? undefined : onClose}
            title="Install accelerator observability"
            subtitle="Prometheus and Grafana are always enabled and reuse the existing Cluster infrastructure stack."
            size="lg"
            footer={footer}
            closeOnBackdrop={!installing}
            closeOnEscape={!installing}
        >
            <div className="space-y-5">
                <div>
                    <Label htmlFor="accelerator-namespace">Namespace</Label>
                    <Input
                        id="accelerator-namespace"
                        value={request.namespace}
                        onChange={(event) => updateRequest({ namespace: event.target.value })}
                        disabled={checking || installing}
                    />
                </div>
                <div>
                    <Label>GPU access mode</Label>
                    <ToggleGroup
                        options={accessModes}
                        value={request.access_mode}
                        onChange={(access_mode) => updateRequest({ access_mode })}
                        fullWidth
                    />
                    <p className="mt-2 text-xs text-theme-muted">
                        Access mode availability is detected from the running Kubernetes environment.
                    </p>
                    {unavailableMessages.length > 0 && (
                        <div className="mt-3 space-y-2">
                            {unavailableMessages.map((message, index) => (
                                <div
                                    key={`${message}-${index}`}
                                    className="flex gap-2 rounded-lg border border-amber-300 dark:border-amber-500/40 bg-amber-50 dark:bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-300"
                                >
                                    <AlertTriangle className="h-4 w-4 shrink-0" />
                                    <span>{message}</span>
                                </div>
                            ))}
                        </div>
                    )}
                </div>

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
                                    <Badge tone={checkTone(check)}>
                                        {checkLabel(check)}
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
