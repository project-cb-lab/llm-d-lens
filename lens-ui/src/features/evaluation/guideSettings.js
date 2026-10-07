export function normalizeGuideSettings(guide, variant, settings = {}) {
    if (!settings || typeof settings !== 'object' || Array.isArray(settings)) throw new Error('Guide settings must be an object.');
    const supported = ['cacheCpuGiB', 'rdmaNicCount', 'routerValues'];
    if (Object.keys(settings).some(key => !supported.includes(key))) throw new Error('Unknown Guide setting.');
    const result = {};
    for (const field of ['cacheCpuGiB', 'rdmaNicCount']) {
        const raw = settings[field];
        if (raw == null || String(raw).trim() === '') continue;
        const value = Number(raw);
        if (!Number.isFinite(value) || value <= 0 || (field === 'rdmaNicCount' && !Number.isSafeInteger(value))) throw new Error(`${field} must be a positive ${field === 'rdmaNicCount' ? 'integer' : 'number'}.`);
        result[field] = value;
    }
    if (result.cacheCpuGiB != null && (guide !== 'tiered-prefix-cache' || !['native/cpu/base', 'lmcache-connector/cpu/base'].includes(variant))) throw new Error('CPU cache capacity requires a supported CPU offload variant.');
    if (result.rdmaNicCount != null && (guide !== 'pd-disaggregation' || variant !== 'vllm-rdma')) throw new Error('NIC count requires the P/D RDMA variant.');
    if (settings.routerValues != null && typeof settings.routerValues !== 'string') throw new Error('Router values must be YAML text.');
    if (settings.routerValues?.trim()) result.routerValues = settings.routerValues.trim();
    return result;
}
