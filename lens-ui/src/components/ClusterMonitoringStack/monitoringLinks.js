export function monitoringLinkTarget(payload, kind, hostname) {
    const link = (payload?.links || []).find((entry) => entry.kind === kind);
    if (!link?.available || !link.local_port) throw new Error(link?.message || `${kind} dashboard is not available`);
    return `http://${hostname}:${link.local_port}${link.dashboard_path || ''}`;
}

export function openMonitoringLink(payload, kind) {
    window.open(monitoringLinkTarget(payload, kind, window.location.hostname), '_blank', 'noopener,noreferrer');
}
