// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import React from 'react';
import { Boxes } from 'lucide-react';

const STATUS_DOT_CLASSES = {
    ready: 'bg-emerald-400',
    external: 'bg-sky-400',
    installing: 'bg-amber-400',
    progressing: 'bg-amber-400',
    pending: 'bg-amber-400',
    absent: 'bg-amber-400',
    degraded: 'bg-red-400',
    unreachable: 'bg-red-400',
    failed: 'bg-red-400',
    inactive: 'bg-slate-500',
    neutral: 'bg-slate-500',
};

function statusDotClass(status) {
    return STATUS_DOT_CLASSES[status] || 'bg-slate-500';
}

function StackStatusLabel({ status, label }) {
    return (
        <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-[10px] font-bold uppercase tracking-wider text-slate-400">
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s] ${statusDotClass(status)}`} aria-hidden="true" />
            {label}
        </span>
    );
}

function StackMetricCard({ icon, title, value, unit, status, detail }) {
    return (
        <div className="flex h-full flex-col gap-3 rounded-xl border border-slate-700/60 bg-slate-800/60 p-4 text-left shadow-lg backdrop-blur-xl">
            <div className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-slate-400">
                    <span className="shrink-0 text-slate-400">{icon}</span>
                    <span className="truncate">{title}</span>
                </div>
                {status && <StackStatusLabel status={status.status} label={status.label} />}
            </div>
            <h3 className="truncate text-3xl font-bold leading-none text-white">
                {value}
                {unit && <span className="ml-1 align-baseline text-sm font-semibold text-slate-400">{unit}</span>}
            </h3>
            {detail && <p className="truncate text-[10px] text-slate-500" title={detail}>{detail}</p>}
        </div>
    );
}

export function ClusterStackSummary({ snapshot }) {
    const components = snapshot?.components || [];
    const managed = components.filter((component) => component.source === 'llmd');
    const external = components.filter((component) => component.source === 'cluster');
    const managedReady = managed.filter((component) => component.status === 'ready').length;
    const externalReady = external.filter((component) => ['ready', 'external'].includes(component.status)).length;

    return (
        <section>
            <div className="mb-3 text-[10px] font-extrabold uppercase tracking-widest text-sky-400/90">Live state</div>
            <div className="grid gap-3 sm:grid-cols-2">
                <StackMetricCard
                    icon={<Boxes size={13} />}
                    title="Managed components"
                    value={managedReady}
                    unit={`/ ${managed.length}`}
                    status={{
                        status: managed.length > 0 && managedReady === managed.length ? 'ready' : 'progressing',
                        label: managed.length > 0 && managedReady === managed.length ? 'ready' : 'partial',
                    }}
                    detail="Installed by the llm-d observability stack"
                />
                <StackMetricCard
                    icon={<Boxes size={13} />}
                    title="External components"
                    value={externalReady}
                    unit={`/ ${external.length}`}
                    status={{
                        status: external.length > 0 && externalReady === external.length ? 'external' : 'progressing',
                        label: external.length > 0 && externalReady === external.length ? 'external' : 'partial',
                    }}
                    detail="Reused from shared cluster-scoped resources"
                />
            </div>
        </section>
    );
}
