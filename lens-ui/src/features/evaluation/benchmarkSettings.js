import YAML from 'js-yaml';

export function applyBenchmarkPreset(preset, current = {}) {
    const matrix = preset === 'long' ? [4096, 8192, 16384].map(isl => ({ isl, osl: 256 })) : [{ isl: preset === 'quick' ? 128 : 1024, osl: preset === 'quick' ? 64 : 128 }];
    const concurrency_stages = (preset === 'throughput' ? [1, 4, 8] : [1]).map(concurrency => ({ concurrency, num_requests: preset === 'quick' ? 10 : (preset === 'throughput' ? Math.max(20, concurrency * 5) : 20) }));
    // Keep the first run short and lightweight; users can opt into larger sweeps explicitly.
    return { parallelism: 1, wait_timeout_seconds: 1800, harness_memory_gib: 8, warmup_requests: 1, ...current,
        harness: 'inference-perf', workload: 'sanity_random.yaml', workload_yaml: null, shared_prefix: null, matrix, concurrency_stages };
}

// Compare the request/load settings so edits immediately show as custom.
export function matchingBenchmarkPreset(benchmark, recommendedBenchmark) {
    const settings = value => JSON.stringify([
        value?.matrix || [], value?.concurrency_stages || [], value?.shared_prefix || null,
        value?.workload_yaml || null, value?.workload || '',
    ]);
    for (const id of ['quick', 'throughput', 'long', 'guide']) {
        const preset = id === 'guide' ? recommendedBenchmark : applyBenchmarkPreset(id, benchmark);
        if (preset && settings(benchmark) === settings(preset)) return id;
    }
    return '';
}

export function benchmarkScenario(benchmark, targets) {
    return { id: 'benchmark', name: 'Configured benchmark', benchmark, sla_targets: Object.fromEntries(Object.entries(targets).filter(([,v]) => v !== '' && v != null)) };
}

export function benchmarkSummary(benchmark, targets) {
    const stages = benchmark.shared_prefix?.stages || benchmark.concurrency_stages || [];
    const points = benchmark.matrix?.length || 1;
    return { points, stages: stages.length, measurements: targets * points * stages.length,
        durationSeconds: benchmark.shared_prefix ? targets * stages.reduce((sum,s) => sum + Number(s.duration || 0),0) : null,
        requests: benchmark.matrix?.length ? targets * points * stages.reduce((sum,s) => sum + Number(s.num_requests || 0),0) : null };
}

export function configurationContextLimit(artifacts) {
    const limits = artifacts.map(a => {
        const c = a.deployable_configuration?.content || {};
        const params = c.customParameters || c.custom_parameters || [];
        const roles = c.prefill ? ['prefill', 'decode'] : ['decode'];
        const roleLimits = roles.map(role => {
            const own = params.findLast(p => p.name === 'max-model-len' && p.target === role);
            const shared = params.findLast(p => p.name === 'max-model-len' && p.target === 'both');
            const config = c[role] || c.serving || {};
            return Number(own?.value || shared?.value || config.maxModelLen || config.max_model_len || c.model?.maxModelLen || 0);
        }).filter(v => v > 0);
        return roleLimits.length ? Math.min(...roleLimits) : 0;
    }).filter(v => v > 0);
    return limits.length ? Math.min(...limits) : 0;
}

export function benchmarkIssues(b, slo = {}, context = 0) {
    const issues = [];
    const number = (value, min, max, label, integer = true) => {
        if (value === '' || value == null || !Number.isFinite(Number(value)) || Number(value) < min || Number(value) > max || (integer && !Number.isInteger(Number(value)))) issues.push(`${label} must be ${integer ? 'an integer' : 'a number'} between ${min} and ${max}.`);
    };
    number(b.parallelism,1,32,'Parallel benchmark instances'); number(b.wait_timeout_seconds,1,14400,'Timeout');
    if (!/^[A-Za-z0-9._-]+$/.test(b.workload || '')) issues.push('Enter a repository workload filename.');
    number(b.harness_memory_gib ?? 32,1,512,'Load generator memory (GiB)');
    number(b.warmup_requests ?? 2,0,50,'Warm-up requests');
    if (b.matrix?.length) {
        if (b.matrix.length > 50) issues.push('At most 50 input/output points are supported.');
        b.matrix.forEach(p => { number(p.isl,1,1000000,'Input tokens'); number(p.osl,1,1000000,'Output tokens'); if(context && Number(p.isl)+Number(p.osl)>context) issues.push(`Input + output exceeds the smallest configured context limit (${context} tokens).`); });
        if (!b.concurrency_stages?.length) issues.push('Add at least one concurrency stage.');
        if (b.concurrency_stages?.length > 20) issues.push('At most 20 concurrency stages are supported.');
        b.concurrency_stages?.forEach(s => { number(s.concurrency,1,4096,'Concurrency'); number(s.num_requests,1,100000,'Requests'); });
    } else if (b.shared_prefix) {
        const p=b.shared_prefix;
        [['num_groups',100000],['num_prompts_per_group',10000],['system_prompt_len',1000000],['question_len',1000000],['output_len',1000000]].forEach(([key,max])=>number(p[key],1,max,key.replaceAll('_',' ')));
        if(context && Number(p.system_prompt_len)+Number(p.question_len)+Number(p.output_len)>context) issues.push(`Request exceeds the smallest configured context limit (${context} tokens).`);
        if(!p.stages?.length) issues.push('Add at least one rate stage.');
        if(p.stages?.length>50) issues.push('At most 50 rate stages are supported.');
        p.stages?.forEach(s=>{number(s.rate,0.001,10000,'Request rate',false);number(s.duration,1,86400,'Stage duration');});
    } else if (b.workload_yaml != null) {
        try { const doc=YAML.load(b.workload_yaml); if(!['load','api','data'].every(k=>doc?.[k] && typeof doc[k]==='object' && !Array.isArray(doc[k]))) issues.push('YAML requires load, api and data mappings.'); else if(!doc.load.stages?.length && !doc.load.sweep) issues.push('YAML requires load stages or a sweep.'); } catch { issues.push('Workload YAML is invalid.'); }
    }
    if(slo.success_rate_min_percent != null) number(slo.success_rate_min_percent,0,100,'Minimum success rate',false);
    for(const key of ['ttft','tpot']) if(slo[`${key}_ms`] !== '' && slo[`${key}_ms`] != null) number(slo[`${key}_ms`],0.001,3600000,key.toUpperCase(),false);
    return [...new Set(issues)];
}
