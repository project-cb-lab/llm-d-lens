export function measurementBaseline(points, run, caseId) {
    const dimensions = ['loadKind', 'load', 'isl', 'osl'];
    if (dimensions.some(key => run[key] == null)) return null;
    const matches = points.filter(point => point.caseId === caseId && dimensions.every(key => point[key] === run[key]));
    return matches.length === 1 ? matches[0] : null;
}

export function percentChange(value, baseline) {
    if (!Number.isFinite(value) || !Number.isFinite(baseline) || baseline === 0) return null;
    const change = (value - baseline) / Math.abs(baseline) * 100;
    return Number.isFinite(change) ? change : null;
}
