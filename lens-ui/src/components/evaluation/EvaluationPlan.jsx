import { createElement } from 'react';
import { Layers, Activity, Server } from 'lucide-react';
import { benchmarkSummary } from '../../features/evaluation/benchmarkSettings';
import { comparisonName } from '../../features/evaluation/domain';

const panelClass = 'rounded-2xl border border-slate-700/60 bg-gradient-to-br from-slate-900/80 to-slate-950/40 p-4 shadow-lg shadow-black/10 sm:p-5';
const editClass = 'rounded-lg border border-slate-700 px-3 py-2 text-xs text-cyan-200 hover:border-cyan-500/50';

export default function EvaluationPlan({
    configurations, baselines, benchmark, slaTargets, cluster, existingEndpoint,
    preserveDeployment, onEditConfigurations, onEditBenchmark, onEditSetup,
}) {
    const existing = Boolean(existingEndpoint);
    const configTargets = configurations.flatMap((configuration, index) => {
        const selected = configuration.optimizationSelection || ['full', ...(index === 0 ? baselines : [])];
        return selected.map(type => ({...configuration, id: `${configuration.id}:${type}`, label: `Configuration ${String.fromCharCode(65 + index)}`, guide: type === 'full' ? configuration.guide : comparisonName(type), reference: type !== 'full', sharedPods: type === 'kubernetes-service'}));
    });
    const targets = existing ? [{ id: 'endpoint', guide: 'Existing endpoint', endpoint: existingEndpoint }]
        : [...configTargets.filter(target => !target.reference || target.sharedPods), ...configTargets.filter(target => target.reference && !target.sharedPods)];
    const matrix = benchmark.matrix || [];
    const prefix = matrix.length ? null : benchmark.shared_prefix;
    const stages = matrix.length ? benchmark.concurrency_stages || [] : prefix?.stages || [];
    const hasStages = Boolean(matrix.length || prefix);
    const counts = benchmarkSummary(benchmark, targets.length);

    return <div className="space-y-4">
        <header className="flex flex-wrap items-start justify-between gap-3">
            <div>
                <h2 className="text-lg font-bold">Execution plan</h2>
                <p className="mt-1 text-xs text-slate-400">Confirm what will run when you press Start Evaluation.</p>
            </div>
            <button type="button" onClick={onEditSetup} className={editClass}>Edit setup</button>
        </header>

        <section className="relative overflow-hidden rounded-2xl border border-cyan-400/25 bg-gradient-to-br from-cyan-500/15 via-sky-500/5 to-slate-950 p-5 sm:p-6">
            <p className="text-lg font-semibold text-cyan-100">{targets.length} benchmark {targets.length === 1 ? 'task' : 'tasks'}{!existing && ' · sequential'}</p>
            <p className="mt-2 text-xs leading-6 text-slate-300">
                {hasStages ? `${targets.length} targets × ${matrix.length || 1} input/output points × ${stages.length} load stages = ${counts.measurements} measured combinations.` : 'Stage count and duration come from the workload.'}
                {counts.requests != null && ` ${counts.requests} measured requests, excluding warm-up.`}
                {prefix && ` ${counts.durationSeconds}s configured load time; excludes deployment, readiness and cleanup.`}
            </p>
            <p className="mt-2 text-xs text-slate-400">Cluster: {cluster || 'Unknown'} · {existing ? 'Benchmark the existing endpoint.' : 'Each target uses the benchmark below.'}</p>
        </section>

        <div className="grid gap-3 sm:grid-cols-3">
            {[{ icon: Layers, label: 'Execution targets', value: targets.length, note: existing ? 'Existing endpoint' : 'Run sequentially' }, { icon: Activity, label: 'Measurements', value: counts.measurements ?? '—', note: 'Across selected load stages' }, { icon: Server, label: 'Target cluster', value: cluster || 'Unknown', note: existing ? 'No deployment required' : 'Deploy → benchmark → collect' }].map(({ icon: Icon, label, value, note }) => <div key={label} className="min-w-0 rounded-xl border border-slate-700/60 bg-slate-900/60 p-4"><div className="flex items-center gap-2 text-xs text-slate-400">{createElement(Icon, { className: 'h-4 w-4 text-cyan-300' })}{label}</div><p className="mt-3 break-words text-xl font-semibold text-slate-100">{value}</p><p className="mt-1 text-xs text-slate-500">{note}</p></div>)}
        </div>

        <section className={panelClass}>
            <div className="flex flex-wrap items-center justify-between gap-3">
                <h3 className="text-sm font-semibold">Targets &amp; execution order</h3>
                {!existing && <button type="button" onClick={onEditConfigurations} className={editClass}>Edit configurations</button>}
            </div>
            <ol className="mt-4 space-y-3">
                {targets.map((target, index) => <li key={target.id} className="flex gap-3 rounded-xl border border-slate-700/60 bg-slate-950/40 p-4">
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl border border-cyan-500/25 bg-cyan-500/10 text-xs font-semibold text-cyan-200">{index + 1}</span>
                    <div className="min-w-0 flex-1 text-xs">
                        <p className="font-semibold text-slate-200">{target.label ? `${target.label} · ` : ''}{target.guide}{target.reference && <span className="ml-2 font-normal text-violet-300">Reference</span>}</p>
                        {target.endpoint ? <p className="mt-1 break-all text-slate-400">{target.endpoint}</p> : target.reference ? <p className="mt-1 text-slate-400">{target.sharedPods ? `Reuse ${target.label} pods through Kubernetes Service.` : 'Derived from this configuration. Uses the same workload and saved serving parameters.'}</p> : <>
                            <p className="mt-1 leading-5 text-slate-300">{target.topology} · {target.gpu || 'Unknown'} accelerators{target.candidate && ` · ${target.candidate}`}</p>
                            <p className="mt-1 break-all leading-5 text-slate-400">{target.model} · {target.image || 'Runtime image unknown'}</p>
                            {target.parameters && <p className="mt-1 break-words leading-5 text-slate-400">{target.parameters}</p>}
                        </>}
                    </div>
                </li>)}
            </ol>
            <p className="mt-4 text-xs leading-6 text-slate-400">{existing
                ? 'Send the configured workload → collect metrics. No deployment is created or cleaned up.'
                : `For each deployment: deploy → wait for readiness → run load stages → collect metrics → ${preserveDeployment ? 'keep successful configuration deployments; clean up independent references' : 'clean up deployment'}.`}</p>
        </section>

        <section className={panelClass}>
            <div className="flex flex-wrap items-center justify-between gap-3">
                <h3 className="text-sm font-semibold">Performance targets</h3>
                <button type="button" onClick={onEditBenchmark} className={editClass}>Edit benchmark</button>
            </div>
            {matrix.length > 0 && <p className="mt-4 text-xs leading-6 text-slate-300">Input → output tokens: {matrix.map(point => `${point.isl} → ${point.osl}`).join(' · ')}. Run every load stage for each token pair.</p>}
            {prefix && <p className="mt-4 text-xs leading-6 text-slate-300">Shared prefix: {prefix.num_groups} groups × {prefix.num_prompts_per_group} samples · {prefix.system_prompt_len} prefix + {prefix.question_len} unique input → {prefix.output_len} output tokens.</p>}
            {hasStages ? <ol className="mt-3 flex flex-wrap gap-2" aria-label="Load stages">
                {stages.map((stage, index) => <li key={index} className="rounded-lg bg-slate-900 px-3 py-2 text-xs text-slate-300"><span className="mr-2 text-slate-500">{index + 1}.</span>{prefix ? `${stage.rate} req/s · ${stage.duration}s` : `${stage.concurrency} concurrent · ${stage.num_requests} requests`}</li>)}
            </ol> : benchmark.workload_yaml ? <details className="mt-4 text-xs text-slate-300"><summary className="cursor-pointer text-cyan-300">View workload YAML</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3">{benchmark.workload_yaml}</pre></details> : <p className="mt-4 text-xs text-slate-300">Repository profile: {benchmark.workload}</p>}
            <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 border-t border-slate-800 pt-4 text-xs text-slate-300">
                <p>Minimum success: {slaTargets.success_rate_min_percent ?? '—'}%</p>
                {['ttft', 'tpot'].map(key => <p key={key}>{key.toUpperCase()}: {slaTargets[`${key}_ms`] ? `${String(slaTargets[`${key}_percentile`] || 'p99').toUpperCase()} ≤ ${slaTargets[`${key}_ms`]} ms` : 'Measure only'}</p>)}
            </div>
            <p className="mt-3 text-xs leading-6 text-slate-500">Timeout: {benchmark.wait_timeout_seconds ?? '—'}s · Parallel benchmark instances: {benchmark.parallelism ?? 1}{matrix.length > 0 && ` · Warm-up: ${benchmark.warmup_requests ?? 2} requests per task`}</p>
        </section>

        <p className="text-xs leading-5 text-slate-500">This plan summarizes your selections. Deployment readiness is checked during execution.</p>
    </div>;
}
