import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import {
    Cpu, GitBranch, Layers, Menu, Network, Play, Plus, RotateCw, Router, Square, Settings, Terminal, Trash2, Waypoints,
} from 'lucide-react';
import { cn } from '../../utils/cn';
import { Badge } from '../ui/Badge';
import { Button } from '../ui/Button';
import { PermissionGate } from '../../features/auth/PermissionGate';

// Canonical llm-d Gateway Mode data plane.
//   Lens control plane           (config publishing)
//            |
//   Clients -> Gateway -> HTTPRoute -> InferencePool -> EPP -> model server
//            |
//   IPP (ext_proc: body.model -> X-Gateway-Base-Model-Name)
//
// Each cluster has its OWN Gateway + IPP, and llm-d's data plane is one EPP per
// InferencePool, so each model service publishes ONE HTTPRoute per cluster that
// references exactly ONE InferencePool (the highest-priority active member).
const COL = { httpRoute: 560, pool: 770, epp: 980, model: 1150 };
const WB = { httpRoute: 200, pool: 180, epp: 140, model: 200 };
const BOX_H = 64;
// Extra vertical gap between rows so connector lines and their captions do not
// touch the boxes above/below.
const ROW_H = 126;
const TOP = 12;
const GW = { x: 300, w: 180, h: 64 };
const IPP = { x: 300, w: 180, h: 64 };
const CP = { x: 300, y: 12, w: 260, h: 54 };
const CLIENTS = { x: 8, y: 12, w: 128, h: 54 };
const CLIENT_BUS = 240;
const CP_TRUNK_X = 270;
const GW_BUS = 520;
const HEADER_H = 30;
const SECTION_GAP = 40;
const WIDTH = COL.model + WB.model + 24;

function Box({ x, y, w = 180, h = BOX_H, label, sub, icon, status, statusText, crd, menuOpen, onToggleMenu }) {
    return (
        <div className="absolute" style={{ left: x, top: y, width: w, height: h }}>
            <div className={cn(
                'flex h-full flex-col justify-center rounded-lg border bg-slate-950/95 px-2.5 shadow-lg',
                menuOpen ? 'border-sky-500/70' : 'border-slate-700/80',
            )}>
                <div className="flex items-center gap-1.5 pr-1">
                    {status && <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse [animation-duration:2.4s]', status)} title={statusText} />}
                    {icon}
                    <span className="text-xs font-semibold text-slate-200">{label}</span>
                    {crd && <Badge tone="neutral" size="xs">CRD</Badge>}
                    {onToggleMenu && (
                        <PermissionGate permission="model-service:gateway:manage">
                            <span className="ml-auto inline-flex">
                                <Button variant="ghost" size="icon" aria-label={`${label} actions`} aria-haspopup="menu" aria-expanded={menuOpen} onClick={onToggleMenu}>
                                    <Menu size={12} />
                                </Button>
                            </span>
                        </PermissionGate>
                    )}
                </div>
                <span className="mt-0.5 truncate text-[9.5px] leading-tight text-slate-500" title={sub}>{sub}</span>
            </div>
        </div>
    );
}

function MenuAction({ icon, label, onClick }) {
    return (
        <button type="button" role="menuitem" onClick={onClick} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-slate-300 hover:bg-slate-800">
            {icon} {label}
        </button>
    );
}

function Flow({ d, dashed = false, dotted = false, tone = 'blue', width = 1.5 }) {
    // blue = token/data path, green = control/config path.
    const stroke = tone === 'green' ? 'stroke-emerald-500' : tone === 'slate' ? 'stroke-slate-600' : 'stroke-blue-500';
    const dash = dotted ? '1 4' : dashed ? '4 4' : undefined;
    return (
        <g>
            <path d={d} fill="none" className={stroke} strokeWidth={width} strokeDasharray={dash}
                opacity={dotted || dashed ? 0.8 : 0.3} />
            {!dotted && <path d={d} fill="none" className={cn('lens-chain-flow', stroke)} strokeWidth={width} opacity="0.95" />}
        </g>
    );
}


function clusterDot(cluster) {
    if (cluster.gatewayState !== 'installed') return 'unhealthy';
    return cluster.gatewayReady ? 'active' : 'degraded';
}

const STATUS_LABEL = {
    active: 'active',
    deployed: 'deployed',
    ready: 'ready',
    running: 'running',
    installed: 'installed',
    missing: 'not installed',
    unhealthy: 'unhealthy',
    degraded: 'degraded',
    external: 'external',
    unknown: 'unknown',
    stopped: 'stopped',
    absent: 'no members',
    disabled: 'disabled',
};

function statusLabel(name) {
    return STATUS_LABEL[name] || name || 'unknown';
}

// Per-component health reported by the backend (healthJson.components) so each
// box reflects its own resource instead of one shared member status.
const COMPONENT_STATE = { ready: 'active', degraded: 'degraded', missing: 'unhealthy', unknown: 'unknown' };

function componentName(member, key) {
    const state = member.components?.[key];
    if (!state || state === 'n/a') return member.status || 'unknown';
    return COMPONENT_STATE[state] || 'unknown';
}

export function DataPlaneTopology({
    clusters = [],
    services = [],
    memberGroups = [],
    statusDot,
    onComponentAction,
    onAddService,
    onAddMemberGlobal,
    onDeleteService,
    onDeleteDeployment,
}) {
    const [menu, setMenu] = useState(null);

    const layout = useMemo(() => {
        const sections = clusters.map((cluster) => {
            const rows = memberGroups.flatMap((group) =>
                group.clusters
                    .filter((gc) => gc.clusterId === cluster.clusterId)
                    .flatMap((gc) => gc.deployments.map((member) => ({
                        serviceId: group.id,
                        serviceName: group.name,
                        member,
                    }))),
            );
            return { cluster, rows };
        });
        const heights = sections.map((section) =>
            Math.max(Math.max(section.rows.length, 1) * ROW_H, GW.h + IPP.h + 12));
        const base = CP.y + CP.h + 40;
        const starts = heights.map((_, index) =>
            base + heights.slice(0, index).reduce((sum, height) => sum + height + SECTION_GAP, 0));
        return sections.map((section, index) => {
            const headerY = starts[index];
            const sectionTop = headerY + HEADER_H;
            const bodyH = heights[index];
            const gwY = sectionTop + (bodyH - GW.h - IPP.h - 12) / 2;
            const ippY = gwY + GW.h + 12;
            const rows = section.rows.map((row, rowIndex) => ({ ...row, y: sectionTop + rowIndex * ROW_H }));
            return { cluster: section.cluster, headerY, gwY, ippY, rows };
        });
    }, [clusters, memberGroups]);

    const totalRows = layout.reduce((sum, section) => sum + section.rows.length, 0);
    // Keep enough room for the always-rendered control plane/clients row even
    // before clusters resolve, so the absolutely positioned boxes are not
    // clipped to a thin strip while the first load is in flight.
    const emptyHeight = CP.y + CP.h + HEADER_H + GW.h + IPP.h + 12;
    const height = Math.max(layout.reduce((max, s) => Math.max(max, s.ippY + IPP.h), 0), emptyHeight) + 24;
    const lastGwMid = layout.length ? layout[layout.length - 1].gwY + GW.h / 2 : 0;

    const toggleMenu = (key, event) => {
        if (menu?.key === key) { setMenu(null); return; }
        const rect = event.currentTarget.getBoundingClientRect();
        setMenu({ key, top: rect.bottom + 4, right: Math.max(8, window.innerWidth - rect.right) });
    };
    const choose = (fn) => { setMenu(null); fn(); };

    useEffect(() => {
        if (!menu) return undefined;
        const close = () => setMenu(null);
        const onKey = (event) => { if (event.key === 'Escape') setMenu(null); };
        window.addEventListener('keydown', onKey);
        window.addEventListener('resize', close);
        window.addEventListener('scroll', close, true);
        return () => {
            window.removeEventListener('keydown', onKey);
            window.removeEventListener('resize', close);
            window.removeEventListener('scroll', close, true);
        };
    }, [menu]);

    const menuItems = () => {
        if (!menu) return null;
        if (menu.key.startsWith('gw:')) {
            const cluster = clusters.find((item) => item.clusterId === menu.key.slice(3));
            if (!cluster) return null;
            const installed = cluster.gatewayState === 'installed';
            return (
                <>
                    <div className="truncate px-2 py-1 text-[10px] uppercase tracking-wide text-slate-500">{cluster.clusterName || cluster.clusterId} · {cluster.gatewayProvider || 'gateway'}</div>
                    <MenuAction icon={installed ? <Trash2 size={12} /> : <Plus size={12} />} label={installed ? 'Uninstall' : 'Install'}
                        onClick={() => choose(() => onComponentAction(cluster, 'gateway', installed ? 'uninstall' : 'install'))} />
                    <MenuAction icon={<RotateCw size={12} />} label="Reconcile" onClick={() => choose(() => onComponentAction(cluster, 'gateway', 'reconcile'))} />
                    <MenuAction icon={cluster.gatewayReady ? <Square size={12} /> : <Play size={12} />} label={cluster.gatewayReady ? 'Stop' : 'Start'}
                        onClick={() => choose(() => onComponentAction(cluster, 'gateway', cluster.gatewayReady ? 'stop' : 'start'))} />
                    <MenuAction icon={<Terminal size={12} />} label="View logs" onClick={() => choose(() => onComponentAction(cluster, 'gateway', 'logs'))} />
                </>
            );
        }
        if (menu.key.startsWith('ipp:')) {
            const cluster = clusters.find((item) => item.clusterId === menu.key.slice(4));
            if (!cluster) return null;
            const installed = cluster.ippState === 'installed';
            return (
                <>
                    <div className="truncate px-2 py-1 text-[10px] uppercase tracking-wide text-slate-500">{cluster.clusterName || cluster.clusterId} · IPP</div>
                    <MenuAction icon={installed ? <Trash2 size={12} /> : <Plus size={12} />} label={installed ? 'Uninstall IPP' : 'Install IPP'}
                        onClick={() => choose(() => onComponentAction(cluster, 'ipp', installed ? 'uninstall' : 'install'))} />
                    <MenuAction icon={installed ? <Square size={12} /> : <Play size={12} />} label={installed ? 'Stop' : 'Start'}
                        onClick={() => choose(() => onComponentAction(cluster, 'ipp', installed ? 'stop' : 'start'))} />
                    <MenuAction icon={<Terminal size={12} />} label="View logs" onClick={() => choose(() => onComponentAction(cluster, 'ipp', 'logs'))} />
                    <MenuAction icon={<Settings size={12} />} label="Configure" onClick={() => choose(() => onComponentAction(cluster, 'ipp', 'configure'))} />
                </>
            );
        }
        if (menu.key.startsWith('route:')) {
            const memberId = menu.key.slice(6);
            const row = layout.flatMap((section) => section.rows).find((item) => item.member.memberId === memberId);
            if (!row) return null;
            const cluster = clusters.find((item) => item.clusterId === row.member.clusterId);
            return (
                <>
                    <div className="truncate px-2 py-1 text-[10px] uppercase tracking-wide text-slate-500">{row.serviceName} · HTTPRoute</div>
                    <MenuAction icon={<RotateCw size={12} />} label="Reconcile route" onClick={() => choose(() => onComponentAction({ clusterId: row.member.clusterId }, 'gateway', 'reconcile'))} />
                    {onDeleteDeployment && (
                        <MenuAction icon={<Trash2 size={12} />} label="Remove provider (this cluster)" onClick={() => choose(() => onDeleteDeployment(row.member, row.serviceName))} />
                    )}
                    {onDeleteService && (
                        <MenuAction icon={<Trash2 size={12} />} label="Remove model service (all clusters)" onClick={() => choose(() => onDeleteService({ id: row.serviceId, name: row.serviceName }, cluster))} />
                    )}
                </>
            );
        }
        if (menu.key.startsWith('epp:')) {
            const memberId = menu.key.slice(4);
            const row = layout.flatMap((section) => section.rows).find((item) => item.member.memberId === memberId);
            if (!row) return null;
            return (
                <>
                    <div className="truncate px-2 py-1 text-[10px] uppercase tracking-wide text-slate-500">{row.member.name}</div>
                    <MenuAction icon={row.member.status === 'active' ? <Square size={12} /> : <Play size={12} />} label={row.member.status === 'active' ? 'Stop' : 'Start'}
                        onClick={() => choose(() => onComponentAction({ clusterId: row.member.clusterId }, 'epp', row.member.status === 'active' ? 'stop' : 'start', null, row.member))} />
                    <MenuAction icon={<Terminal size={12} />} label="View logs" onClick={() => choose(() => onComponentAction({ clusterId: row.member.clusterId }, 'epp', 'logs', null, row.member))} />
                </>
            );
        }
        return null;
    };

    return (
        <div className="flex w-full flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
                {onAddService && (
                    <PermissionGate permission="model-service:group:manage">
                        <Button variant="secondary" size="xs" onClick={onAddService}><Plus size={12} /> Add model service</Button>
                    </PermissionGate>
                )}
                {onAddMemberGlobal && (
                    <PermissionGate permission="model-service:member:manage">
                        <Button variant="secondary" size="xs" onClick={onAddMemberGlobal}><Plus size={12} /> Add model server</Button>
                    </PermissionGate>
                )}
                <span className="ml-auto text-[10px] text-slate-500">
                    {clusters.length} cluster(s) · {services.length} model service(s) · {totalRows} route(s)
                </span>
            </div>

            <div className="w-full shrink-0 overflow-x-auto">
                <div className="relative" style={{ width: WIDTH, height, minWidth: WIDTH }}>
                    <svg className="pointer-events-none absolute inset-0" width={WIDTH} height={height} viewBox={`0 0 ${WIDTH} ${height}`}>
                        {layout.length > 0 && (
                            <>
                                <Flow d={`M ${CLIENTS.x + CLIENTS.w} ${CLIENTS.y + CLIENTS.h / 2} H ${CLIENT_BUS}`} width={2.6} />
                                <Flow d={`M ${CLIENT_BUS} ${CLIENTS.y + CLIENTS.h / 2} V ${lastGwMid - 14}`} width={2.6} />
                                {layout.map((section) => (
                                    <Flow key={`c-${section.cluster.clusterId}`} width={2.6} d={`M ${CLIENT_BUS} ${section.gwY + GW.h / 2 - 14} H ${GW.x}`} />
                                ))}
                                <Flow tone="green" d={`M ${CP.x} ${CP.y + CP.h / 2} H ${CP_TRUNK_X}`} />
                                <Flow tone="green" d={`M ${CP_TRUNK_X} ${CP.y + CP.h / 2} V ${lastGwMid + 14}`} />
                                {layout.map((section) => (
                                    <Flow key={`cp-${section.cluster.clusterId}`} tone="green"
                                        d={`M ${CP_TRUNK_X} ${section.gwY + GW.h / 2 + 14} H ${GW.x}`} />
                                ))}
                            </>
                        )}
                        {layout.map((section) => {
                            const gwMid = section.gwY + GW.h / 2;
                            const gwRight = GW.x + GW.w;
                            return (
                                <g key={section.cluster.clusterId}>
                                    <Flow tone="green" d={`M ${IPP.x + IPP.w / 2} ${section.ippY} V ${section.gwY + GW.h}`} />
                                    <Flow width={2.6} d={`M ${gwRight} ${gwMid} H ${GW_BUS}`} />
                                    {section.rows.map((row, rowIndex) => (
                                        <g key={row.member.memberId}>
                                            {/* Config: Gateway resolves the model's HTTPRoute, whose
                                                backendRef points at the InferencePool (no data flows here). */}
                                            <Flow tone="green" d={`M ${GW_BUS} ${gwMid} V ${row.y + BOX_H / 2} H ${COL.httpRoute}`} />
                                            <Flow tone="green" d={`M ${COL.httpRoute + WB.httpRoute} ${row.y + BOX_H / 2} H ${COL.pool}`} />
                                            {/* InferencePool's endpointPickerRef -> EPP (config). */}
                                            <Flow tone="green" d={`M ${COL.pool + WB.pool} ${row.y + BOX_H / 2} H ${COL.epp}`} />
                                            {/* InferencePool selector picks the model-server pods (config). */}
                                            <Flow tone="green" d={`M ${COL.pool + WB.pool / 2} ${row.y + BOX_H} V ${row.y + BOX_H + 26} H ${COL.model + WB.model / 2} V ${row.y + BOX_H}`} />
                                            {/* ext_proc: the pool's EPP picks the endpoint. */}
                                            <Flow tone="green" d={`M ${GW_BUS} ${gwMid} V ${row.y + BOX_H + 12} H ${COL.epp + WB.epp / 2} V ${row.y + BOX_H}`} />
                                            {/* data: Gateway forwards to the EPP-chosen pod (ORIGINAL_DST). */}
                                            <Flow width={2.6} d={`M ${GW_BUS} ${gwMid} V ${row.y - 12} H ${COL.model + WB.model / 2} V ${row.y}`} />
                                            {rowIndex === 0 && (
                                                <>
                                                </>
                                            )}
                                        </g>
                                    ))}
                                </g>
                            );
                        })}
                    </svg>

                    <Box x={CP.x} y={CP.y} w={CP.w} h={CP.h} label="Lens control plane" icon={<Waypoints size={13} className="shrink-0 text-slate-400" />} status="bg-emerald-400" statusText="Lens control plane: connected" sub="tokens · permissions · model catalog" />
                    <Box x={CLIENTS.x} y={CLIENTS.y} w={CLIENTS.w} h={CLIENTS.h} label="Clients / SDK" icon={<Waypoints size={13} className="shrink-0 text-slate-400" />} sub="Gateway URL + token" />

                    {layout.map((section) => {
                        const cluster = section.cluster;
                        const ippRunning = cluster.ippState === 'installed';
                        const gwState = clusterDot(cluster);
                        return (
                            <div key={cluster.clusterId}>
                                <div className="absolute text-[10px] font-semibold uppercase tracking-wide text-slate-500" style={{ left: GW.x, top: section.headerY }}>
                                    {cluster.clusterName || cluster.clusterId}
                                </div>
                                <Box x={GW.x} y={section.gwY} w={GW.w} label="Gateway" icon={<Network size={13} className="shrink-0 text-slate-400" />}
                                    status={statusDot[gwState] || statusDot.unknown}
                                    statusText={`Gateway: ${cluster.gatewayState !== 'installed' ? 'not installed' : cluster.gatewayReady ? 'ready' : 'installed, not ready'} (${statusLabel(gwState)})`}
                                    sub={`${cluster.clusterName || cluster.clusterId} · ${cluster.gatewayProvider || 'gateway'} · ${cluster.gatewayReady ? 'ready' : cluster.gatewayState}`}
                                    menuOpen={menu?.key === `gw:${cluster.clusterId}`} onToggleMenu={(event) => toggleMenu(`gw:${cluster.clusterId}`, event)} />
                                <Box x={IPP.x} y={section.ippY} w={IPP.w} label="IPP" icon={<Waypoints size={13} className="shrink-0 text-slate-400" />}
                                    status={statusDot[ippRunning ? 'active' : 'missing'] || statusDot.unknown}
                                    statusText={`IPP: ${ippRunning ? 'running' : statusLabel(cluster.ippState)}`}
                                    sub={`${cluster.clusterName || cluster.clusterId} · ext_proc ${ippRunning ? 'running' : cluster.ippState}`}
                                    menuOpen={menu?.key === `ipp:${cluster.clusterId}`} onToggleMenu={(event) => toggleMenu(`ipp:${cluster.clusterId}`, event)} />
                                {section.rows.map((row) => {
                                    const name = (key) => componentName(row.member, key);
                                    const dot = (key) => statusDot[name(key)] || statusDot.unknown;
                                    const eppKey = `epp:${row.member.memberId}`;
                                    const routeKey = `route:${row.member.memberId}`;
                                    return (
                                        <div key={row.member.memberId}>
                                            <Box x={COL.httpRoute} y={row.y} w={WB.httpRoute} label="HTTPRoute" icon={<GitBranch size={13} className="shrink-0 text-slate-400" />} crd
                                                status={dot('httpRoute')} statusText={`HTTPRoute: ${statusLabel(name('httpRoute'))}`} sub={row.serviceName}
                                                menuOpen={menu?.key === routeKey} onToggleMenu={(event) => toggleMenu(routeKey, event)} />
                                            <Box x={COL.pool} y={row.y} w={WB.pool} label="InferencePool" icon={<Layers size={13} className="shrink-0 text-slate-400" />} crd
                                                status={dot('inferencePool')} statusText={`InferencePool: ${statusLabel(name('inferencePool'))}`} sub={row.member.poolName || row.serviceName} />
                                            <Box x={COL.epp} y={row.y} w={WB.epp} label="EPP" icon={<Router size={13} className="shrink-0 text-slate-400" />} status={dot('epp')}
                                                statusText={`EPP: ${statusLabel(name('epp'))}`} sub={row.member.eppRef || '—'} menuOpen={menu?.key === eppKey} onToggleMenu={(event) => toggleMenu(eppKey, event)} />
                                            <Box x={COL.model} y={row.y} w={WB.model} label="model server" icon={<Cpu size={13} className="shrink-0 text-slate-400" />} status={dot('modelServer')}
                                                statusText={`model server: ${statusLabel(name('modelServer'))}`} sub={row.member.name} />
                                        </div>
                                    );
                                })}
                            </div>
                        );
                    })}

                    {clusters.length === 0 && (
                        <div className="absolute text-xs text-slate-400" style={{ left: GW.x, top: CP.y + CP.h + 40 }}>
                            No cluster with model services yet
                        </div>
                    )}
                </div>
            </div>

            {menu && typeof document !== 'undefined' && createPortal(
                <>
                    <button type="button" aria-label="Close menu" className="fixed inset-0 z-[100] cursor-default" onClick={() => setMenu(null)} />
                    <div role="menu" style={{ top: menu.top, right: menu.right }} className="fixed z-[110] w-56 rounded-lg border border-slate-700/60 bg-slate-900 p-1 shadow-2xl">
                        {menuItems()}
                    </div>
                </>,
                document.body,
            )}
        </div>
    );
}

export default DataPlaneTopology;
