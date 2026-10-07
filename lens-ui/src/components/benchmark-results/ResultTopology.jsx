import { topologyMetricDefinitions as metricDefinitions } from './metricMetadata.js';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, Boxes, Database, Layers, Maximize2, Minimize2, Move, Network, RotateCcw, Scan, Search, Server, X, ZoomIn, ZoomOut } from 'lucide-react';
import { SideDrawer } from '../ui/SideDrawer.jsx';
import { Button } from '../ui/Button';

const finite = value => typeof value === 'number' && Number.isFinite(value);
const format = value => new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value);
const roleLabel = role => ({ prefill: 'Prefill', decode: 'Decode', router: 'Router', epp: 'EPP', 'model-server': 'Unclassified pod', pod: 'Unclassified pod' }[role] || role || 'Pod');
const list = value => Array.isArray(value) ? value : [];

// eslint-disable-next-line react-refresh/only-export-components
export function topologyNodeMetrics(node, mode) {
    if (node.id === 'index') return metricsOf(node).slice(0, 3);
    const keys = mode === 'token' ? ['output_token_rate_tps', 'input_token_rate_tps', 'inflight_token_load'] : ['request_rate_rps', 'running_requests', 'waiting_requests'];
    const metrics = metricsOf(node);
    return keys.flatMap(key => metrics.filter(metric => metric.key === key));
}

function metricsOf(node) {
    return Object.entries(node.metrics || {}).flatMap(([key, entry]) => {
        const definition = metricDefinitions[key];
        if (!definition) return [];
        const value = finite(entry) ? entry : entry?.mean;
        if (!finite(value)) return [];
        return [{ key, label: definition[0], unit: definition[1], value, stat: finite(entry) ? 'Saved value' : key.includes('_p95_') ? 'Mean of sampled rolling P95 values; not global P95' : 'Window mean', source: `${node.source}.${key}`, max: entry?.max }];
    });
}

/** Relationships describe guide architecture only; no request routing is inferred from counters. */
// eslint-disable-next-line react-refresh/only-export-components
export function buildResultTopology(run = {}, guideType) {
    const observation = run.observability || {};
    const caseObservation = run.caseObservability || {};
    const podsFromCase = !list(observation.per_pod).length && !!list(caseObservation.per_pod).length;
    const pods = list(podsFromCase ? caseObservation.per_pod : observation.per_pod);
    const resourcePods = list(run.resources?.pods);
    const endpointsFromCase = !list(observation.per_endpoint).length && !!list(caseObservation.per_endpoint).length;
    const endpoints = list(endpointsFromCase ? caseObservation.per_endpoint : observation.per_endpoint);
    const identities = new Map();
    for (const resource of resourcePods) {
        const name = resource.name || resource.metadata?.name;
        if (name) identities.set(name, { id: `pod:${name}`, label: name, role: resource.role || resource.labels?.['llm-d.ai/role'] || 'pod', resource, metrics: {}, source: 'resource_snapshot.pods', scope: 'Case-wide resource snapshot', capturedAt: run.resources?.captured_at, status: 'Saved resource' });
    }
    for (const pod of pods) {
        const name = pod.pod || pod.name;
        if (!name) continue;
        const previous = identities.get(name) || {};
        identities.set(name, { ...previous, id: `pod:${name}`, label: name, role: pod.role || previous.role || 'pod', metrics: pod, source: `${podsFromCase ? 'case_observability' : 'observability'}.per_pod[${name}]`, scope: podsFromCase ? 'Case-wide snapshot' : run.scopeLabel || 'Saved measurement window', window: (podsFromCase ? caseObservation : observation).window, status: 'Observed pod' });
    }
    const actualNodes = [...identities.values(), ...endpoints.filter(entry => entry.endpoint).map(entry => ({ id: `endpoint:${entry.endpoint}`, label: entry.endpoint, role: 'endpoint', metrics: entry, source: `${endpointsFromCase ? 'case_observability' : 'observability'}.per_endpoint[${entry.endpoint}]`, scope: endpointsFromCase ? 'Case-wide snapshot' : run.scopeLabel || 'Saved measurement window', status: 'Observed endpoint' }))];
    const nodes = [];
    const edges = [];
    const logical = (id, label, column, extra = {}) => ({ id, label, column, role: id, status: 'Logical component', source: 'Guide architecture', scope: 'Logical relationship only', ...extra });
    const connect = (from, to, label = 'Logical relationship', event = false) => edges.push({ from, to, label, event, kind: 'logical' });
    if (guideType === 'tiered-prefix-cache') {
        const config = run.configuration || {};
        const settings = config.guideSettings || config.guide_settings || {};
        const variant = config.guide_variant || config.guideVariant || config.variant || '';
        const cpuBudget = config.cacheCpuGiB ?? settings.cacheCpuGiB;
        const summary = observation.summary || caseObservation.summary || {};
        const cache = observation.cache_config || caseObservation.cache_config || {};
        const cpuKnown = (finite(cpuBudget) && cpuBudget > 0) || /(?:^|\/)cpu(?:\/|$)/.test(variant) || finite(cache.cpu_capacity_tokens) || finite(summary.cpu_cache_usage_percent?.mean);
        const fsKnown = /(?:^|\/)(?:fs|filesystem)(?:\/|$)/.test(variant) || !!config.filesystem_cache_path;
        nodes.push(logical('hbm', 'HBM KV cache', 0, { description: 'Logical cache tier; device memory is not KV capacity.' }));
        if (cpuKnown) { nodes.push(logical('cpu', 'CPU RAM', 1, { status: 'Configured / evidenced tier', source: 'Saved configuration / cache telemetry', resource: { guide_variant: variant || undefined, cacheCpuGiB: cpuBudget }, description: 'Capacity scope follows the backend configuration; tier presence does not prove reuse.' })); connect('hbm', 'cpu', 'Offload / restore architecture'); }
        if (fsKnown) { nodes.push(logical('filesystem', 'Filesystem', 2, { status: 'Configured tier', resource: { guide_variant: variant }, description: 'Configured storage tier; successful cache restores require separate telemetry.' })); connect(cpuKnown ? 'cpu' : 'hbm', 'filesystem', 'Storage hierarchy'); }
        actualNodes.forEach(node => nodes.push({ ...node, column: 3 }));
    } else {
        nodes.push(logical('entry', 'Service entry', 0, { description: 'Logical entry point, not a measured router instance.' }));
        // Namespace snapshots include benchmark and monitoring pods. The collector's
        // model-server role is a fallback, so it is not positive serving identity.
        const hasServingIdentity = node => node.status === 'Observed endpoint' || (node.status === 'Observed pod' && ['prefill', 'decode', 'router', 'epp'].includes(node.role));
        const hasModelIdentity = node => node.status === 'Observed pod' && ['prefill', 'decode'].includes(node.role);
        actualNodes.forEach(node => {
            const identified = hasServingIdentity(node);
            const column = identified ? (guideType === 'pd-disaggregation' && node.role === 'decode' ? 2 : 1) : 3;
            nodes.push({ ...node, column, ...(!identified ? { description: 'Saved namespace object. Serving role and request/event relationships are unverified; no connection is inferred.' } : {}) });
            if (identified) connect('entry', node.id);
        });
        if (!actualNodes.some(hasModelIdentity) && guideType !== 'pd-disaggregation') {
            nodes.push(logical('model-pool', 'Model serving pool', 1, { description: 'Guide architecture only. Saved namespace objects have not been mapped to this serving pool.' }));
            connect('entry', 'model-pool', 'Guide serving architecture');
        }
        if (guideType === 'precise-prefix-cache-routing') {
            nodes.push(logical('index', 'KV index', 2, { metrics: observation.router || caseObservation.router || {}, source: observation.router ? 'observability.router' : 'case_observability.router', scope: observation.router ? run.scopeLabel || 'Saved measurement window' : caseObservation.router ? 'Case-wide snapshot' : 'Logical relationship only', description: 'Logical index component. Saved index counters do not establish per-pod event coverage.' }));
            connect('index', 'entry', 'Index lookup', true);
            actualNodes.filter(hasModelIdentity).forEach(node => connect(node.id, 'index', 'KV event architecture; publisher activity unverified', true));
            if (nodes.some(node => node.id === 'model-pool')) connect('model-pool', 'index', 'Guide KV event architecture; no observed publisher mapping', true);
        }
    }
    return { nodes, edges, actualCount: actualNodes.length, caseWide: !!run.stage && (podsFromCase || endpointsFromCase || resourcePods.length > 0 || nodes.some(node => node.scope === 'Case-wide snapshot')) };
}

function Inspector({ node, onClose, onInspectEvidence }) {
    const [tab, setTab] = useState('metrics');
    const metrics = metricsOf(node);
    return <aside aria-label={`${node.label} inspector`} className="min-w-0">
        <div className="flex items-start justify-between gap-2"><div className="min-w-0"><p className="text-[10px] uppercase tracking-wider text-violet-300">Inspect {roleLabel(node.role)}</p><h4 className="mt-1 break-all text-sm font-semibold text-slate-100">{node.label}</h4></div><button type="button" aria-label="Close topology inspector" onClick={onClose} className="rounded p-1 text-slate-400 hover:bg-slate-800 focus-visible:outline focus-visible:outline-violet-400"><X size={16} /></button></div>
        <p className="mt-2 text-[11px] text-amber-200/80">{node.scope}</p>
        {node.window?.start && node.window?.end && <p className="mt-1 break-all text-[10px] text-slate-500">{node.window.start} → {node.window.end}</p>}
        {node.resource && <p className="mt-1 text-[10px] text-slate-500">{['cpu', 'filesystem'].includes(node.id) ? 'Saved cache configuration' : 'Resources: case-wide Kubernetes snapshot'}{node.capturedAt ? ` · ${node.capturedAt}` : ''}</p>}
        <div className="my-4 flex gap-2" role="group" aria-label="Node evidence view">{['metrics', 'resources'].map(value => <button key={value} type="button" aria-pressed={tab === value} onClick={() => setTab(value)} className={`rounded-md border px-3 py-1.5 text-xs ${tab === value ? 'border-violet-400/40 bg-violet-400/10 text-violet-200' : 'border-slate-700 text-slate-400'}`}>{value === 'metrics' ? 'Inspect metrics' : 'View resources'}</button>)}</div>
        {tab === 'metrics' ? <div className="space-y-3">{metrics.length ? metrics.map(metric => <div key={metric.key} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3"><div className="flex justify-between gap-2 text-xs"><span className="text-slate-400">{metric.label}</span><b className="text-slate-100">{format(metric.value)} <span className="font-normal text-slate-400">{metric.unit}</span></b></div><p className="mt-1 text-[10px] text-slate-500">{metric.stat}{finite(metric.max) ? ` · max ${format(metric.max)} ${metric.unit}` : ''}</p><p className="mt-1 break-all font-mono text-[9px] text-slate-500">{metric.source}</p></div>) : <p className="text-xs leading-5 text-slate-400">No numeric telemetry was saved for this object. Its presence does not establish health, utilization or request flow.</p>}</div> : <div className="text-xs text-slate-400">{node.resource ? <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-950 p-3 text-[10px]">{JSON.stringify(node.resource, null, 2)}</pre> : <p>No resource snapshot was saved for this object.</p>}</div>}
        {node.description && <p className="mt-4 text-[11px] leading-5 text-slate-400">{node.description}</p>}
        {node.metrics && Object.keys(node.metrics).length > 0 && <details className="mt-4 text-[11px] text-slate-400"><summary className="cursor-pointer">Saved raw evidence</summary><pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap break-all text-[10px]">{JSON.stringify(node.metrics, null, 2)}</pre></details>}
        {onInspectEvidence && <button type="button" onClick={() => onInspectEvidence(node)} className="mt-4 flex items-center gap-2 text-xs text-violet-300"><Search size={12} />Inspect related evidence</button>}
    </aside>;
}

function CanvasTool({ label, icon, onClick, disabled }) {
    const Icon = icon;
    return <Button type="button" variant="ghost" size="icon" aria-label={label} title={label} onClick={onClick} disabled={disabled} className="h-8 w-8 rounded-full border border-slate-700/60 bg-slate-900/60 text-slate-400 hover:bg-slate-800 hover:text-slate-100"><Icon size={15} /></Button>;
}

export default function ResultTopology({ run = {}, guideType, onInspectEvidence }) {
    const graph = useMemo(() => buildResultTopology(run, guideType), [run, guideType]);
    const [hovered, setHovered] = useState(null);
    const [selection, setSelection] = useState(null);
    const [metricMode, setMetricMode] = useState('request');
    const [zoom, setZoom] = useState(1);
    const [fullscreen, setFullscreen] = useState(false);
    const canvasRef = useRef(null);
    const sectionRef = useRef(null);
    const dragRef = useRef(null);
    useEffect(() => {
        if (!fullscreen) return undefined;
        const previousFocus = document.activeElement;
        const previousOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        sectionRef.current?.focus();
        const onKey = event => {
            if (event.key === 'Escape') { setFullscreen(false); return; }
            if (event.key !== 'Tab') return;
            const controls = [...(sectionRef.current?.querySelectorAll('button:not([disabled]), [tabindex="0"], summary') || [])];
            const first = controls[0], last = controls[controls.length - 1];
            if (event.shiftKey && (document.activeElement === first || document.activeElement === sectionRef.current)) { event.preventDefault(); last?.focus(); }
            if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        };
        document.addEventListener('keydown', onKey);
        return () => { document.body.style.overflow = previousOverflow; document.removeEventListener('keydown', onKey); previousFocus?.focus?.(); };
    }, [fullscreen]);
    const selected = selection && selection.runId === run.id ? graph.nodes.find(node => node.id === selection.nodeId) : null;
    const focus = hovered || selected?.id;
    const adjacent = new Set([focus]);
    graph.edges.filter(edge => edge.from === focus || edge.to === focus).forEach(edge => { adjacent.add(edge.from); adjacent.add(edge.to); });
    const columns = [...new Set(graph.nodes.map(node => node.column))].sort((a, b) => a - b);
    const width = Math.max(660, columns.length * 260);
    const height = Math.max(330, Math.max(...columns.map(column => graph.nodes.filter(node => node.column === column).length)) * 158 + 76);
    const positions = new Map();
    columns.forEach((column, index) => {
        const members = graph.nodes.filter(node => node.column === column);
        members.forEach((node, row) => positions.set(node.id, { x: 34 + index * ((width - 280) / Math.max(1, columns.length - 1)), y: height / 2 - members.length * 79 + row * 158 + 16 }));
    });
    const tiered = guideType === 'tiered-prefix-cache';
    const fitView = () => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        setZoom(Math.max(0.2, Math.min(1, (canvas.clientWidth - 16) / width, (canvas.clientHeight - 16) / height)));
        canvas.scrollTo({ left: 0, top: 0 });
    };
    const resetView = () => { setZoom(1); setHovered(null); setSelection(null); canvasRef.current?.scrollTo({ left: 0, top: 0 }); };
    return <section ref={sectionRef} tabIndex={fullscreen ? -1 : undefined} role={fullscreen ? 'dialog' : undefined} aria-modal={fullscreen || undefined} aria-label={fullscreen ? 'Benchmark topology fullscreen' : undefined} className={`overflow-hidden border border-slate-800 bg-[#090e18] ${fullscreen ? 'fixed inset-0 z-[100] flex flex-col' : 'rounded-2xl'}`}>
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-800/70 p-5"><div><h3 className="flex items-center gap-2 text-sm font-semibold text-slate-100"><Network size={16} className="text-violet-300" />{tiered ? 'Cache hierarchy & saved instances' : 'Deployment relationship explorer'}</h3><p className="mt-1 text-xs text-slate-500">Hover to focus relationships. Select an object to inspect its saved evidence.</p></div><span className="rounded-full border border-slate-700 px-2.5 py-1 text-[10px] text-slate-400">{graph.actualCount} saved objects</span></div>
        {graph.caseWide && <p className="border-b border-amber-500/15 bg-amber-500/5 px-5 py-2.5 text-xs text-amber-200/90">Case-wide snapshot — pod summaries or resources are not restricted to the selected stage. The inspector identifies each object's scope.</p>}
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 pt-3"><div className="inline-flex gap-1 rounded-lg border border-slate-700/70 bg-slate-900/60 p-1" role="group" aria-label="Flow metric">{[['request', 'Requests'], ['token', 'Tokens']].map(([value, label]) => <Button key={value} type="button" variant="ghost" size="xs" aria-pressed={metricMode === value} onClick={() => setMetricMode(value)} className={metricMode === value ? 'bg-violet-400/15 text-violet-200 hover:bg-violet-400/20 hover:text-violet-100' : 'text-slate-500 hover:bg-slate-800 hover:text-slate-200'}>{label}</Button>)}</div><div className="flex items-center gap-1"><span aria-live="polite" className="mr-2 min-w-10 text-right font-mono text-[10px] text-slate-500">{zoom.toFixed(1)}×</span>
            <CanvasTool label="Zoom in" icon={ZoomIn} onClick={() => setZoom(value => Math.min(3, value + 0.2))} disabled={zoom >= 3} />
            <CanvasTool label="Zoom out" icon={ZoomOut} onClick={() => setZoom(value => Math.max(0.2, value - 0.2))} disabled={zoom <= 0.2} />
            <CanvasTool label="Fit view" icon={Scan} onClick={fitView} />
            <CanvasTool label="Reset view" icon={RotateCcw} onClick={resetView} />
            <CanvasTool label={fullscreen ? 'Exit fullscreen' : 'Fullscreen canvas'} icon={fullscreen ? Minimize2 : Maximize2} onClick={() => setFullscreen(value => !value)} />
        </div></div>
        <div className={`flex min-h-0 flex-col gap-3 p-3 lg:flex-row ${fullscreen ? 'flex-1' : ''}`}><div ref={canvasRef} tabIndex={0} aria-label="Scrollable topology canvas" onPointerDown={event => {
            if (event.button !== 0 || event.target.closest('button')) return;
            dragRef.current = { x: event.clientX, y: event.clientY, left: event.currentTarget.scrollLeft, top: event.currentTarget.scrollTop };
            event.currentTarget.setPointerCapture(event.pointerId);
        }} onPointerMove={event => {
            const start = dragRef.current;
            if (!start) return;
            event.currentTarget.scrollLeft = start.left + start.x - event.clientX;
            event.currentTarget.scrollTop = start.top + start.y - event.clientY;
        }} onPointerUp={() => { dragRef.current = null; }} onPointerCancel={() => { dragRef.current = null; }} className={`min-h-0 min-w-0 flex-1 cursor-grab overflow-auto rounded-xl border border-slate-800/70 bg-[#0b1120] active:cursor-grabbing ${fullscreen ? '' : 'h-[460px] max-h-[560px]'}`}><div style={{ width: width * zoom, height: height * zoom, margin: '0 auto' }}><div className="relative origin-top-left" style={{ width, height, transform: `scale(${zoom})`, backgroundImage: 'radial-gradient(#33415555 1px, transparent 1px)', backgroundSize: '20px 20px' }}>
            <svg aria-hidden="true" width={width} height={height} className="absolute inset-0">{graph.edges.map(edge => {
                const from = positions.get(edge.from), to = positions.get(edge.to);
                const forward = from.x <= to.x;
                const x1 = from.x + (forward ? 212 : 0), x2 = to.x + (forward ? 0 : 212);
                const y1 = from.y + 42, y2 = to.y + 42, middle = (x1 + x2) / 2;
                const active = !focus || edge.from === focus || edge.to === focus;
                return <g key={`${edge.from}:${edge.to}`} opacity={active ? 0.7 : 0.12}><title>{edge.label}</title><path d={`M${x1},${y1} C${middle},${y1} ${middle},${y2} ${x2},${y2}`} stroke={edge.event ? '#a78bfa' : '#64748b'} strokeWidth="1.5" strokeDasharray={edge.event ? '4 5' : '7 4'} fill="none" /></g>;
            })}</svg>
            {graph.nodes.map(node => {
                const position = positions.get(node.id);
                const nodeMetrics = topologyNodeMetrics(node, metricMode);
                const Icon = ['hbm', 'cpu', 'filesystem', 'index'].includes(node.id) ? Database : node.id === 'entry' ? Network : node.role === 'endpoint' ? Activity : Server;
                const active = !focus || adjacent.has(node.id);
                return <button key={node.id} type="button" aria-label={`Inspect ${node.label}`} aria-pressed={selected?.id === node.id} onMouseEnter={() => setHovered(node.id)} onMouseLeave={() => setHovered(null)} onFocus={() => setHovered(node.id)} onBlur={() => setHovered(null)} onClick={() => setSelection({ runId: run.id, nodeId: node.id })} onKeyDown={event => { if (event.key === 'Escape') { setSelection(null); setHovered(null); } }} className={`absolute w-[212px] rounded-xl border p-3 text-left transition-opacity focus-visible:outline focus-visible:outline-2 focus-visible:outline-violet-400 ${selected?.id === node.id ? 'border-violet-400 bg-[#211d38] shadow-lg shadow-violet-500/10' : 'border-slate-700 bg-[#101827] hover:border-violet-400/60'}`} style={{ left: position.x, top: position.y, opacity: active ? 1 : 0.25 }}>
                    <div className="flex items-center gap-2"><Icon size={15} className="shrink-0 text-violet-300" /><span className="truncate text-xs font-medium text-slate-100" title={node.label}>{node.label}</span></div>
                    <p className="mt-1.5 truncate text-[9px] text-slate-500">{node.status}{node.role && !['entry', 'index', 'hbm', 'cpu', 'filesystem'].includes(node.role) ? ` · ${roleLabel(node.role)}` : ''}</p>
                    {nodeMetrics.length > 0 ? <div className="mt-2 space-y-1.5 border-t border-slate-700/60 pt-2">{nodeMetrics.map(metric => <p key={metric.key} className="flex items-center justify-between gap-2 text-[9px] text-slate-400" title={`${metric.stat} · ${metric.source}`}><span className="truncate">{metric.label}</span><b className="shrink-0 font-mono text-slate-200">{format(metric.value)} {metric.unit}</b></p>)}<p className="text-[8px] text-slate-600">Saved window statistics</p></div> : <p className="mt-2 border-t border-slate-700/60 pt-2 text-[9px] text-slate-600">No saved {metricMode === 'token' ? 'token' : 'request'} metrics</p>}
                </button>;
            })}
        </div></div></div>{selected && <SideDrawer title={selected.label} subtitle="Topology · saved node evidence" onClose={() => setSelection(null)}><Inspector key={`${run.id}:${selected.id}`} node={selected} onClose={() => setSelection(null)} onInspectEvidence={onInspectEvidence} /></SideDrawer>}</div>
        {!graph.actualCount && <p className="px-5 pb-3 text-xs text-slate-400">No pod or endpoint evidence was saved. Only logical or explicitly configured components are shown.</p>}
        <div className="flex shrink-0 flex-wrap items-center gap-x-5 gap-y-2 border-t border-slate-800/60 px-5 py-3 text-[10px] text-slate-500"><span className="flex items-center gap-2"><Move size={12} />Drag the canvas to pan</span><span className="flex items-center gap-2"><Layers size={12} />Logical relationships — not a request trace; line width does not encode traffic.</span><span className="flex items-center gap-2"><Boxes size={12} />Unclassified and resource-only objects remain unconnected.</span></div>
    </section>;
}
