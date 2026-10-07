import { latencyFamilies, clientMetrics, telemetryMetrics, routerMetrics } from './metricMetadata.js';
import { readGuideMetric } from './guideMetricValues.js';
/** Persisted result adapters. Never promote case telemetry to a stage without a window. */
const finite = (value) => typeof value === 'number' && Number.isFinite(value) ? value : null;
const object = (value) => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const array = (value) => Array.isArray(value) ? value : [];
const validWindow = (value) => value?.start && value?.end && Number.isFinite(Date.parse(value.start)) && Date.parse(value.end) > Date.parse(value.start);
const mean = (value) => finite(value?.mean);

export const GUIDE_EXPLORER_PROFILES = {
    'optimized-baseline': {
        title: 'Cache affinity & load balance',
        question: 'Does prefix reuse improve TTFT without overloading an instance?',
        description: 'Compare the full affinity and token-load policy with a recorded load-only control under the same traffic, hardware and warm-up.',
        evidenceKeys: ['cacheHit', 'tokenLoadCv', 'queue'],
        limitations: ['Prefix group × Pod and spillover decisions require request-to-destination correlation.', 'Endpoint window-mean CV is not same-time token-load CV; heterogeneous pools require capacity normalization.', 'Engine cache counter units depend on the exporter version; this ratio is not a request hit rate.'],
    },
    'precise-prefix-cache-routing': {
        title: 'Precise index & actual reuse',
        question: 'Does precise cache state reduce recomputation relative to approximate routing?',
        description: 'Inspect event/index activity separately from engine reuse. Compare the same completed input set, tokenizer, block size and warm-up.',
        evidenceKeys: ['actualCachedPromptFraction', 'actualRecomputedTokens', 'engineCompute', 'engineRecompute', 'engineLocalReuse', 'engineExternalReuse', 'indexLookup', 'indexLookups', 'indexAdmissions', 'indexEvictions', 'subscribers', 'cachedPromptFraction', 'recomputedTokens'],
        limitations: ['EPP cached-token signals are router estimates, not actual engine reuse.', 'Subscriber count alone cannot prove publisher coverage or recovery.', 'Matched blocks are not requests; index activity alone does not prove a latency benefit.'],
    },
    'tiered-prefix-cache': {
        title: 'Cache pressure & restore cost',
        question: 'When HBM cannot retain the working set, is restoring KV cheaper than recomputing it?',
        description: 'Compare HBM-only with the same backend and routing policy. Inspect actual restore activity, TTFT and host/storage cost together.',
        evidenceKeys: ['workingSet', 'configuredPrefixScale', 'hbmCapacity', 'hbmUsage', 'hbmPeak', 'cpuCapacity', 'cpuUsage', 'restoreTime', 'offload', 'restore', 'offloadedBytes', 'restoredBytes', 'externalCacheHit', 'cpuMemory'],
        limitations: ['Distinct reusable KV working set is not measured by cumulative input tokens.', 'HBM and CPU capacity sums are not deduplicated effective cache capacity.', 'External cache hits are not attributed to CPU versus filesystem; do not stack conditional hit rates.'],
    },
    'pd-disaggregation': {
        title: 'Prefill / Decode balance',
        question: 'Does separating Prefill stabilize generation, and what is the TTFT and transfer cost?',
        description: 'Compare combined and PD deployments at matched hardware budgets. Keep TTFT, ITL and TPOT distinct and inspect both role queues.',
        evidenceKeys: ['prefillQueue', 'decodeQueue', 'prefillGpu', 'decodeGpu', 'transferLatency', 'queue', 'transferRate', 'transferFailures', 'transferBandwidth'],
        limitations: ['Configured P/D roles do not prove the path of an individual request; handoff traces require collection.', 'Connector failures and completed transfers do not necessarily share an attempts denominator.', 'Interference needs aligned burst cohorts and token timing; cross-stage P95 comparisons do not establish causality.'],
    },
};

/** Select the evaluated candidate Guide before any comparison baseline. */
export function resolveExplorerGuide(details) {
    const entries = array(details?.cases);
    const candidates = entries.filter((entry) => (entry.case || entry).kind !== 'baseline');
    const baselines = entries.filter((entry) => (entry.case || entry).kind === 'baseline');
    for (const entry of [...candidates, ...baselines]) {
        const record = entry.case || entry;
        const choices = [record.deployment_configuration?.provider_ref, record.configuration?.guide, record.spec?.guide, record.guide, ...array(entry.deployment_cases).map((deployment) => deployment.guide)];
        const guide = choices.find((choice) => Object.hasOwn(GUIDE_EXPLORER_PROFILES, choice));
        if (guide) return guide;
    }
    return [details?.workflow?.guide, details?.workflow?.harness].find((guide) => Object.hasOwn(GUIDE_EXPLORER_PROFILES, guide)) || 'optimized-baseline';
}

export function buildExplorerRuns(details) {
    if (!details) return [];
    const entries = Array.isArray(details.cases) ? details.cases : details.evaluation ? [{ case: { ...details.workflow, ...details.evaluation }, evaluation: details.evaluation }] : [];
    const runs = [];
    for (const [caseIndex, entry] of entries.entries()) {
        const record = object(entry.case || entry);
        const evaluation = object(entry.evaluation);
        const embedded = object(record.deployment_configuration);
        const configuration = Object.keys(embedded).length ? { ...object(embedded.content), provider_ref: embedded.provider_ref, embedded } : object(record.configuration || record.spec);
        const benchmark = object(record.benchmark || configuration.benchmark || evaluation.benchmark);
        const caseMetrics = object(record.metrics || evaluation.metrics);
        const caseObservability = object(caseMetrics.observability || record.observability || evaluation.observability);
        const caseId = String(record.id || evaluation.id || `case-${caseIndex}`);
        const guide = embedded.provider_ref || configuration.guide || record.guide || caseObservability.guide_type || details.workflow?.guide || details.workflow?.harness || 'unknown';
        const model = typeof configuration.model === 'string' ? configuration.model : configuration.model?.name;
        const baseLabel = record.name || `${model || 'Model'} · ${record.baseline_type || guide}`;
        const common = {
            caseId, guide, caseName: record.name || null, baselineType: record.baseline_type || null, kind: record.kind || 'guide', configuration: { ...configuration, benchmark }, caseObservability,
            // Detail API may refresh deployment_cases resource snapshots from live Kubernetes.
            // Only the case-completion snapshot is evidence of the recorded evaluation.
            resources: record.resource_snapshot || null,
            resourcesScope: 'Persisted case-completion snapshot; not stage-window telemetry',
            resourcesReason: record.resource_snapshot ? '' : 'No persisted case resource snapshot. Live deployment snapshots are excluded from historical evidence.',
        };
        const append = (data, suffix, isStage, dimensions = {}) => {
            const metrics = dimensions.telemetryOnly ? {} : (isStage || dimensions.pointSummary) ? object(data.metrics) : caseMetrics;
            const ownObservability = object(data.observability || metrics.observability);
            // A legacy rate-stage window is persisted by the backend from duration alignment.
            // Its bounds are useful, but not proof of exact request traffic timestamps.
            const observation = dimensions.pointSummary ? {} : isStage ? (validWindow(ownObservability.window) ? ownObservability : {}) : caseObservability;
            const window = validWindow(observation.window) ? observation.window : validWindow(data.window) ? data.window : null;
            const withinWindow = (points) => array(points).filter((point) => {
                const time = Date.parse(point.timestamp);
                return window && Number.isFinite(time) && time >= Date.parse(window.start) && time <= Date.parse(window.end);
            });
            const observability = isStage && validWindow(observation.window) ? {
                ...observation,
                series: withinWindow(array(observation.series).length ? observation.series : caseObservability.series),
                role_series: withinWindow(array(observation.role_series).length ? observation.role_series : caseObservability.role_series),
            } : observation;
            // Harness parallelism counts traffic workers, not concurrent requests.
            const loadKind = dimensions.caseSummary ? null : dimensions.loadKind ?? null;
            const load = dimensions.caseSummary ? null : finite(dimensions.load);
            const loadText = load === null ? '' : ` · ${load} ${loadKind === 'rate' ? 'offered req/s' : 'concurrent'}`;
            runs.push({ ...common, id: `${caseId}${suffix}`, label: `${baseLabel}${dimensions.pointSummary ? ` · Full length point · ISL ${dimensions.isl} / OSL ${dimensions.osl}` : dimensions.caseSummary ? ' · Full case evidence' : ''}${dimensions.isl != null ? ` · ISL ${dimensions.isl} / OSL ${dimensions.osl ?? '—'}` : ''}${loadText}`, loadKind, load, isl: finite(dimensions.isl ?? benchmark.isl), osl: finite(dimensions.osl ?? benchmark.osl), metrics, observability, stage: isStage, window, scopeLabel: dimensions.pointSummary ? 'Length point · full run across load stages' : isStage ? (validWindow(observation.window) ? 'Stage · persisted telemetry window' : 'Stage · client summary only') : dimensions.telemetryOnly ? 'Case · full telemetry window; client aggregate unavailable' : 'Case · full run' });
        };
        const rateStages = array(record.rate_stage_results || evaluation.rate_stage_results);
        const matrix = array(record.matrix_results || evaluation.matrix_results);
        if (rateStages.length) {
            rateStages.forEach((stage, index) => append(stage, `:rate-stage-${index}`, true, { loadKind: 'rate', load: stage.rate }));
            append(record, ':case-summary', false, { caseSummary: true });
        } else if (matrix.length) {
            matrix.forEach((point, pointIndex) => {
                const stages = array(point.stage_metrics);
                (stages.length ? stages : [point]).forEach((stage, index) => append(stage, `:matrix-${pointIndex}:stage-${index}`, true, { loadKind: 'concurrency', load: stage.concurrency, isl: point.isl, osl: point.osl }));
                if (point.metrics?.distinct_kv_working_set) append(point, `:matrix-${pointIndex}:summary`, false, {caseSummary: true, pointSummary: true, isl: point.isl, osl: point.osl});
            });
            // Matrix _metric_summary selects the latest lifecycle file, not a global aggregate.
            append(record, ':case-summary', false, { caseSummary: true, telemetryOnly: true });
        } else {
            append(record, '', false);
        }
    }
    return runs;
}

export function getRunSeries(run) {
    const obs = object(run?.observability);
    const window = obs.window || run?.window;
    if (!validWindow(window)) return [];
    const start = Date.parse(window.start), end = Date.parse(window.end);
    const byTime = new Map();
    for (const point of [...array(obs.series), ...array(obs.role_series)]) {
        const time = Date.parse(point.timestamp);
        if (!Number.isFinite(time) || time < start || time > end) continue;
        byTime.set(time, { ...byTime.get(time), ...point, timestamp: new Date(time).toISOString(), elapsed: (time - start) / 1000 });
    }
    return [...byTime.entries()].sort(([a], [b]) => a - b).map(([, point]) => point);
}

/** Return the exact statistic recorded, or explain why it cannot be reconstructed. */
export function readResultMetric(run, key, stat = 'p95') {
    const metrics = object(run?.metrics), obs = object(run?.observability);
    const scope = run?.scopeLabel || (run?.stage ? 'Stage' : 'Case');
    const definitions = {
        ttft: 'Time from sending a request until its first response token.',
        itl: 'Time between successive output tokens as reported by the benchmark harness.',
        ntpot: 'Request latency normalized by output-token count, as recorded by the harness; distinct from TPOT and ITL.',
        tpot: 'Per-request average output-token time as reported by the harness; different from individual inter-token gaps.',
        e2e: 'Time from sending a request until its response completes.',
        throughput: 'Reported output tokens generated per second during the client measurement window.',
        requestRate: 'Reported achieved request throughput; not the configured offered request rate.',
        successRate: 'Percentage of benchmark requests recorded as successful. Execution completion is not an SLO verdict.',
        requestCount: 'Number of requests counted by the benchmark harness.',
        successCount: 'Requests the harness recorded as successful.',
        failureCount: 'Requests the harness recorded as failed.',
        duration: 'Client measurement duration, which can include draining requests after the offered-load stage.',
    };
    let field = latencyFamilies[key] ? `metrics.latency_distributions.${latencyFamilies[key][0]}.${stat}_ms`
        : clientMetrics[key] ? `metrics.${clientMetrics[key][0]}`
        : telemetryMetrics[key] ? `observability.summary.${telemetryMetrics[key][0]}.mean`
        : routerMetrics[key] ? `observability.router.${routerMetrics[key][0]}.mean` : null;

    if (key === 'hbmUsage' && mean(obs.summary?.gpu_cache_usage_percent) === null && mean(obs.summary?.kv_cache_usage_percent) !== null) field = 'observability.summary.kv_cache_usage_percent.mean';
    if (routerMetrics[key] && mean(obs.router?.[routerMetrics[key][0]]) === null && mean(obs.summary?.[`router_${routerMetrics[key][0]}`]) !== null) field = `observability.summary.router_${routerMetrics[key][0]}.mean`;
    const make = (value, label, unit, source, quality = 'observed', reason = '', formula) => ({ value: finite(value), unit, label, source, quality: finite(value) === null ? 'unavailable' : quality, reason: finite(value) === null ? reason || 'No finite sample was saved for this result and scope.' : reason, scope, field, definition: definitions[key], resultLabel: run?.label || run?.caseId, window: run?.window || obs.window || null, ...(formula ? { formula } : {}) });
    if (latencyFamilies[key]) {
        const [family, label] = latencyFamilies[key];
        const distribution = metrics.latency_distributions?.[family];
        // Current parser writes flat *_ms as medians, but legacy records are ambiguous.
        // Only explicitly named statistics are safe across result schema versions.
        const value = finite(distribution?.[`${stat}_ms`]);
        return make(value, `${label} ${stat === 'mean' ? 'mean' : stat.toUpperCase()}`, 'ms', 'Client benchmark latency summary', 'observed', value === null ? `The ${stat} statistic was not recorded; other percentiles cannot substitute for it.` : '');
    }
    if (clientMetrics[key]) {
        const [field, label, unit] = clientMetrics[key];
        return make(metrics[field], label, unit, 'Client benchmark summary');
    }
    if (key === 'goodput') {
        const evidence = metrics.request_slo_goodput;
        const valid = evidence?.schema_version === 1 && evidence.status === 'measured'
            && finite(evidence.value) !== null && evidence.value >= 0
            && finite(evidence.duration_seconds) > 0;
        return {...make(valid ? evidence.value : null, 'Per-request SLO goodput', 'req/s', evidence?.source || 'Requires successful request records and a measured window', 'calculated', evidence?.reason || 'Collect per-request lifecycle reports and configure TTFT or TPOT targets. Legacy capacity goodput is not request-level goodput.', evidence?.formula),
            field: 'metrics.request_slo_goodput', scope: {...(typeof scope === 'object' ? scope : {label: scope}), constraints_seconds: evidence?.constraints_seconds, good_requests: evidence?.good_requests, request_count: evidence?.request_count, duration_seconds: evidence?.duration_seconds}};
    }
    const guideMetric = readGuideMetric(run, key, make);
    if (guideMetric) return guideMetric;
    if (key === 'offloadedBytes' || key === 'restoredBytes') {
        const offload = key === 'offloadedBytes';
        const field = offload ? 'kv_offload_bytes_per_second' : 'kv_restore_bytes_per_second';
        const rate = mean(obs.summary?.[field]);
        const duration = validWindow(obs.window) ? (Date.parse(obs.window.end) - Date.parse(obs.window.start)) / 1000 : null;
        const value = rate !== null && rate >= 0 && duration !== null ? rate * duration : null;
        return make(value, `${offload ? 'Offloaded' : 'Restored'} bytes · sampled-rate estimate`, 'bytes', `Prometheus · ${field} · persisted window`, 'estimated', 'Sampled mean rate × window duration is approximate integration, not an exact counter delta.', 'mean(sampled bytes/s) × persisted window seconds');
    }
    if (key === 'workingSet') {
        const evidence = metrics.distinct_kv_working_set;
        const valid = evidence?.schema_version === 1 && evidence.status === 'measured'
            && finite(evidence.value) !== null && evidence.value >= 0 && validWindow(evidence.window);
        const label = evidence?.measurement_scope === 'completed-full-prompt-blocks' ? 'Distinct prompt KV working set · completed requests' : 'Distinct reusable KV working set';
        return {...make(valid ? evidence.value : null, label, 'tokens', evidence?.source || 'Requires KV block identity and revisit window', 'calculated', valid ? (evidence?.measurement_scope === 'completed-full-prompt-blocks' ? 'Complete prompt blocks held at successful request completion; excludes generated tokens, partial tails and failed requests.' : 'Unique blocks in the recorded access window.') : evidence?.reason || 'Collect a complete context-aware KV block access trace; configured prefix scale is not a measurement.', evidence?.formula), field: 'metrics.distinct_kv_working_set', window: valid ? evidence.window : null};
    }
    if (key === 'configuredPrefixScale') {
        const prefix = run?.configuration?.benchmark?.shared_prefix;
        const groups = finite(prefix?.num_groups), length = finite(prefix?.system_prompt_len);
        return {...make(groups > 0 && length > 0 ? groups * length : null, 'Configured prefix scale · estimate', 'tokens', 'Saved shared-prefix workload configuration', 'estimated', 'Configured prefix groups × shared length; not distinct runtime KV blocks or cache occupancy.', 'num_groups × system_prompt_len'), field: 'configuration.benchmark.shared_prefix'};
    }
    if (key === 'hbmCapacity') return make(obs.cache_config?.hbm_capacity_tokens, 'Runtime HBM KV token capacity · reported aggregate', 'tokens', 'vLLM cache_config_info', 'observed', 'Reported aggregate capacity is not deduplicated reusable capacity.');
    if (telemetryMetrics[key]) {
        const [field, label, unit] = telemetryMetrics[key];
        const value = mean(obs.summary?.[field]) ?? (key === 'hbmUsage' ? mean(obs.summary?.kv_cache_usage_percent) : null);
        return make(value, label, unit, `Prometheus · ${field} · window sample mean`, 'observed', key === 'cacheHit' ? 'Engine hit/query counter ratio; confirm token/block units for the exporter version. Not a request hit rate.' : key === 'externalCacheHit' ? 'External hits / external queries, averaged over sampled rate ratios. Conditional external-cache denominator; not CPU/FS attribution and not additive with HBM hit ratio.' : '');
    }
    if (routerMetrics[key]) {
        const [field, label, unit] = routerMetrics[key];
        return make(mean(obs.router?.[field]) ?? mean(obs.summary?.[`router_${field}`]), label, unit, `Prometheus · router.${field} · window sample mean`, 'observed', key === 'indexLookup' ? 'Mean of sampled rolling histogram P95 values, not a global request P95.' : '');
    }
    if (key === 'tokenLoadCv') {
        const loads = array(obs.per_endpoint).map((item) => mean(item.inflight_token_load)).filter((value) => value !== null);
        const average = loads.length ? loads.reduce((a, b) => a + b, 0) / loads.length : 0;
        const value = loads.length > 1 && average > 0 ? Math.sqrt(loads.reduce((sum, v) => sum + (v - average) ** 2, 0) / loads.length) / average : null;
        return make(value, 'Token-load CV · endpoint window means', 'CV', 'EPP per-endpoint in-flight tokens', 'calculated', 'Not simultaneous load CV; endpoint coverage and heterogeneous capacity normalization are unverified.', 'population standard deviation(endpoint window means) / mean(endpoint window means)');
    }
    if (key === 'cachedPromptFraction' || key === 'recomputedTokens') {
        const input = mean(obs.router?.input_token_rate_tps) ?? mean(obs.summary?.router_input_token_rate_tps);
        const cached = mean(obs.router?.cached_token_rate_tps) ?? mean(obs.summary?.router_cached_token_rate_tps);
        const valid = input !== null && input > 0 && cached !== null && cached >= 0 && cached <= input;
        const seconds = validWindow(obs.window) ? (Date.parse(obs.window.end) - Date.parse(obs.window.start)) / 1000 : null;
        const fraction = key === 'cachedPromptFraction';
        const value = valid && (fraction || seconds !== null) ? (fraction ? cached / input * 100 : (input - cached) * seconds) : null;
        return make(value, fraction ? 'Cached prompt fraction · ROUTER ESTIMATE' : 'Recomputed tokens · ROUTER ESTIMATE', fraction ? '%' : 'tokens', 'EPP cached/input token rate samples', 'estimated', 'Router estimates do not establish actual engine reuse or recomputation. Rate means are not exact counter deltas.', fraction ? 'mean(router cached token rate) / mean(router input token rate) × 100' : '(mean(router input rate) − mean(router cached rate)) × window seconds');
    }
    return make(null, key, '', 'No supported recorded source', 'unavailable', 'This metric has no supported source in the saved result.');
}
