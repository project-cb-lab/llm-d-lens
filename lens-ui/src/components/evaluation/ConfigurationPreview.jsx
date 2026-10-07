// Copyright 2026 Google LLC
// SPDX-License-Identifier: Apache-2.0

import { ChevronRight, FileCode } from 'lucide-react';

// Render tokens as React text so manifest contents are never interpreted as HTML.
function yamlLine(line) {
    return line.split(/("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|(?:^|\s)#[^\n]*$|[\w./-]+(?=:\s|:$)|\b(?:true|false|null|\d+(?:\.\d+)?)\b)/g).map((token, index) => {
        const color = /^\s*#/.test(token) ? 'text-slate-500' : /^["']/.test(token) ? 'text-emerald-300' : /^(true|false|null|\d+(\.\d+)?)$/.test(token) ? 'text-amber-300' : /^[\w./-]+$/.test(token) ? 'text-cyan-300' : 'text-slate-300';
        return <span key={index} className={color}>{token}</span>;
    });
}

export function YamlPreview({ content = '', title = 'Deployment YAML', defaultOpen = true, prominent = false }) {
    return <details open={defaultOpen} className={`group/yaml mt-3 overflow-hidden rounded-xl border ${prominent ? 'border-cyan-500/40 shadow-lg shadow-cyan-950/10' : 'border-slate-700/60'}`}>
        <summary className={`flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-xs font-medium [&::-webkit-details-marker]:hidden ${prominent ? 'bg-cyan-500/10 text-cyan-200' : 'bg-slate-900/40 text-slate-300'}`}>
            <ChevronRight className="h-3.5 w-3.5 shrink-0 transition-transform group-open/yaml:rotate-90" aria-hidden="true" />
            <FileCode className="h-4 w-4 shrink-0" aria-hidden="true" /><span className="min-w-0 break-all">{title}</span>
            <span className="ml-auto text-[10px] font-normal text-slate-400">YAML</span>
        </summary>
        <pre aria-label={title === 'Deployment YAML' ? 'Generated YAML' : title} tabIndex={0} className="max-h-[36rem] overflow-auto bg-slate-950 p-4 font-mono text-xs leading-6 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-cyan-400"><code>{content.split('\n').map((line, index, lines) => <span key={index}>{yamlLine(line)}{index < lines.length - 1 ? '\n' : ''}</span>)}</code></pre>
    </details>;
}

export function TopologyPreview({ variants, isPd, onChange }) {
    if (!variants.length) return null;
    const editable = Boolean(onChange) && variants.length === 1;
    const showConfigurationNumber = !editable;
    const inputClass = 'h-8 w-16 border border-slate-700 bg-slate-950 px-2 text-xs text-slate-100 outline-none focus:border-cyan-500';
    return <section aria-label="Topology preview" className="mt-4 border-t border-slate-700/60 pt-4">
        <div className="mb-3 flex items-center justify-between gap-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wide text-cyan-300">Topology preview</h3>
            <span className="text-[10px] text-slate-400">{editable ? 'Single configuration' : `${variants.length} configuration${variants.length === 1 ? '' : 's'}`}</span>
        </div>
        <div className="overflow-x-auto">
            <table aria-label="Topology configurations" className="w-full whitespace-nowrap text-left text-xs tabular-nums">
                <thead className="border-b border-slate-700/60 bg-slate-900/40 text-[10px] text-slate-400">
                    <tr>
                        {showConfigurationNumber && <th scope="col" className="px-3 py-2 font-medium">Configuration</th>}
                        {isPd && <><th scope="col" className="px-3 py-2 font-medium">Prefill replicas</th><th scope="col" className="px-3 py-2 font-medium">Prefill TP</th></>}
                        <th scope="col" className="px-3 py-2 font-medium">{isPd ? 'Decode replicas' : 'Replicas'}</th>
                        <th scope="col" className="px-3 py-2 font-medium">{isPd ? 'Decode TP' : 'TP'}</th>
                        <th scope="col" className="px-3 py-2 text-right font-medium">Accelerators</th>
                    </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60">
                    {variants.map(row => <tr key={row.id} className="text-slate-300 transition-colors hover:bg-cyan-500/5">
                        {showConfigurationNumber && <th scope="row" className="px-3 py-2.5 font-medium text-cyan-300">#{row.id}</th>}
                        {isPd && <><td className="px-3 py-2.5">{editable ? <input aria-label="Prefill replicas" type="number" min="1" step="1" className={inputClass} value={row.prefillReplicaCount} onChange={(event) => onChange({ prefillReplicas: event.target.value })} /> : row.prefillReplicaCount}</td><td className="px-3 py-2.5">{editable ? <input aria-label="Prefill tensor parallel size" type="number" min="1" step="1" className={inputClass} value={row.prefillTpCount} onChange={(event) => onChange({ prefillTensorParallelSize: event.target.value })} /> : row.prefillTpCount}</td></>}
                        <td className="px-3 py-2.5">{editable ? <input aria-label={isPd ? 'Decode replicas' : 'Replicas'} type="number" min="1" step="1" className={inputClass} value={row.replicaCount} onChange={(event) => onChange({ replicas: event.target.value })} /> : row.replicaCount}</td>
                        <td className="px-3 py-2.5">{editable ? <input aria-label={isPd ? 'Decode tensor parallel size' : 'Tensor parallel size'} type="number" min="1" step="1" className={inputClass} value={row.tpCount} onChange={(event) => onChange({ tensorParallelSize: event.target.value })} /> : row.tpCount}</td>
                        <td className="px-3 py-2.5 text-right">{row.gpuCount}</td>
                    </tr>)}
                </tbody>
            </table>
        </div>
    </section>;
}
