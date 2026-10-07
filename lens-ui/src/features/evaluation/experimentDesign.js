/** Only expose combinations backed by deployable provider profiles. */
export function optimizationOptions(guide) {
    const full = { id: 'full', label: 'Full Guide', summary: 'Selected Guide configuration', components: {
        'optimized-baseline': ['EPP', 'Prefix-cache aware', 'Load-aware'],
        'pd-disaggregation': ['EPP', 'Prefill / Decode separation', 'KV transfer'],
        'tiered-prefix-cache': ['EPP', 'Prefix caching', 'KV cache offload / restore'],
        'precise-prefix-cache-routing': ['EPP', 'Precise prefix-cache index', 'Cache-aware routing'],
    }[guide] || ['Guide defaults'] };
    const options = [full];
    if (['optimized-baseline','pd-disaggregation','tiered-prefix-cache','precise-prefix-cache-routing'].includes(guide)) options.push(
        { id: 'router-neutral', label: 'EPP · Random routing', summary: 'Combined serving reference', components: ['EPP', 'Neutral random picker'] },
        { id: 'affinity-only', label: 'Prefix-cache aware', summary: 'EPP · Combined serving reference', components: ['EPP', 'Approximate prefix affinity; no token-load scorer; saturation protection remains'] },
        { id: 'load-only', label: 'Load-aware', summary: 'EPP · Combined serving reference', components: ['EPP', 'Token-load scorer; no prefix affinity'] },
    );
    if (['pd-disaggregation', 'tiered-prefix-cache', 'precise-prefix-cache-routing'].includes(guide)) options.push({ id: 'optimized-baseline', label: 'Optimized Baseline', summary: 'Prefix + load · combined serving', components: ['EPP', 'Prefix-cache aware', 'Load-aware', 'Separate reference deployment; no P/D separation, external cache tier, or precise cache index'] });
    if (guide === 'precise-prefix-cache-routing') options.push({ id: 'kubernetes-service', label: 'Bypass EPP', summary: 'Same pods · Kubernetes Service', components: ['Kubernetes Service', 'Reuses Full Guide deployment and cache'] });
    options.push({ id: 'direct-vllm', label: 'Direct vLLM', summary: 'Separate deployment · no EPP', components: ['Direct serving', 'No EPP routing; vLLM prefix caching is not necessarily disabled'] });
    return options;
}
export function selectedOptimizationPlan(guide, selected = ['full']) {
    const allowed = new Set(optimizationOptions(guide).map(item => item.id));
    if (!selected.length) throw new Error('Select at least one optimization combination per configuration.');
    if (selected.some(id => !allowed.has(id))) throw new Error('Selected optimization is unsupported by this Guide.');
    if (selected.includes('kubernetes-service') && !selected.includes('full')) throw new Error('The same-pod reference requires Full Guide.');
    const baseline_types = [...new Set(selected.filter(id => id !== 'full'))];
    return { include_configuration: selected.includes('full'), include_baseline: baseline_types.length > 0, baseline_types };
}
export function recommendationBudget(hardware, basis = 'available') {
    const value = basis === 'total' ? hardware?.gpuCount : hardware?.usableGpuCount ?? hardware?.availableGpuCount;
    return Number.isFinite(Number(value)) && Number(value) >= 0 ? Number(value) : 0;
}

/** Compose supported deployment capabilities without exposing profile IDs to users. */
export function compileOptimizationComponents(guide, {epp=false,prefix=false,load=false,mechanism=false,samePods=false}) {
    if (!epp && (prefix || load || mechanism)) throw new Error('Prefix affinity, load scoring and Guide mechanisms require EPP.');
    if (samePods) {
        if (guide !== 'precise-prefix-cache-routing' || epp) throw new Error('Same-pod bypass requires precise cache routing with EPP bypassed.');
        return 'kubernetes-service';
    }
    if (!epp) return 'direct-vllm';
    if (mechanism && guide !== 'optimized-baseline') {
        if (!prefix || !load) throw new Error('This Guide mechanism requires its bundled prefix and load policy; independent ablation is available with the mechanism off.');
        return 'full';
    }
    if (prefix && load) return guide === 'optimized-baseline' ? 'full' : 'optimized-baseline';
    return prefix ? 'affinity-only' : load ? 'load-only' : 'router-neutral';
}
export function toggleOptimizationSelection(selected, id) {
    let next=selected.includes(id)?selected.filter(value=>value!==id):[...selected,id];
    if(id==='full' && !next.includes('full'))next=next.filter(value=>value!=='kubernetes-service');
    if(next.includes('kubernetes-service') && !next.includes('full'))next.push('full');
    return next;
}

export function withRecommendationBudget(request, hardware, basis) {
    return { ...request, resourceBasis: basis, recommendationGpuCount: recommendationBudget(hardware, basis) };
}
