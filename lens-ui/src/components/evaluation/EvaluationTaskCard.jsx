import React from 'react';
import { taskProgress } from './taskProgress';
import { Activity, ArrowUpRight, CheckCircle2, CircleDashed, Clock3, Layers3, RotateCcw, Square, Trash2 } from 'lucide-react';

const terminal = new Set(['succeeded', 'failed', 'cancelled']);
const active = new Set(['queued', 'running', 'rendering', 'deploying', 'benchmarking']);
const guides = { 'optimized-baseline': 'Optimized baseline', 'pd-disaggregation': 'PD disaggregation', 'precise-prefix-cache-routing': 'Precise prefix routing', 'tiered-prefix-cache': 'Tiered prefix cache' };
const finite = value => typeof value === 'number' && Number.isFinite(value);
const number = value => new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }).format(value);
const unique = values => [...new Set(values.filter(Boolean))];
const modelName = value => typeof value === 'string' ? value : value?.name;
const date = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : '—';
const range = values => !values.length ? '—' : Math.min(...values) === Math.max(...values) ? number(values[0]) : `${number(Math.min(...values))}–${number(Math.max(...values))}`;

function workloadFacts(benchmark) {
    // Match _execute_benchmark generator precedence. A dormant workload filename
    // is not the traffic source when an inline/generated workload is active.
    if (benchmark.matrix?.length) return { label: 'Token-length matrix', matrix: benchmark.matrix, concurrency: benchmark.concurrency_stages || [], rates: [] };
    if (benchmark.shared_prefix && Object.keys(benchmark.shared_prefix).length) return { label: 'Shared-prefix traffic', matrix: [], concurrency: [], rates: benchmark.shared_prefix.stages || [] };
    if (benchmark.workload_yaml) return { label: 'Custom YAML', matrix: [], concurrency: [], rates: [] };
    return { label: benchmark.workload, matrix: [], concurrency: [], rates: [] };
}

function facts(item) {
    const config = item.configuration || item.spec || {};
    const content = item.deployment_configuration?.content || {};
    const decode = content.decode || content.serving || {};
    const prefill = content.prefill || {};
    const replica = config.decode_replicas ?? config.replicas ?? decode.replicaCount;
    const tp = config.decode_tensor_parallel_size ?? config.tensor_parallel_size ?? decode.tensorParallelSize;
    const pReplica = config.prefill_replicas ?? prefill.replicaCount;
    return {
        guide: config.guide || item.deployment_configuration?.provider_ref,
        model: modelName(config.model) || modelName(content.model),
        topology: pReplica != null ? `P ${pReplica}×TP${config.prefill_tensor_parallel_size ?? prefill.tensorParallelSize ?? '—'} · D ${replica ?? '—'}×TP${tp ?? '—'}` : replica != null || tp != null ? `${replica ?? '—'} replicas · TP${tp ?? '—'}` : null,
        benchmark: item.benchmark || config.benchmark || {},
    };
}

function recordedResults(record) {
    if (record.rate_stage_results?.length) return record.rate_stage_results.map(stage => ({ metrics: stage.metrics, scope: 'stage' }));
    if (record.matrix_results?.length) return record.matrix_results.flatMap(point => point.stage_metrics?.length
        ? point.stage_metrics.map(stage => ({ metrics: stage.metrics, scope: 'stage' }))
        : [{ metrics: point.metrics, scope: 'point' }]);
    return [{ metrics: record.metrics, scope: 'summary' }];
}

export default function EvaluationTaskCard({ task, onOpen, onAction }) {
    const workflow = task.kind === 'workflow';
    const progress = taskProgress(task);
    const cases = Array.isArray(task.cases) ? task.cases : [];
    const configurationFacts = cases.filter(item => item.kind !== 'baseline').map(facts);
    const models = unique([...configurationFacts.map(item => item.model), modelName(task.model), modelName(task.model_name), ...cases.map(item => modelName(item.model_name) || modelName(item.model)), ...(task.readyDeployments || []).map(item => modelName(item.model) || modelName(item.model_name))]);
    const guideNames = unique(configurationFacts.map(item => guides[item.guide] || item.guide));
    const benchmarks = workflow && cases.length ? cases.map(item => facts(item).benchmark) : [task.benchmark || task];
    const traffic = benchmarks.map(workloadFacts);
    const workloads = unique(traffic.map(item => item.label));
    const concurrency = unique(traffic.flatMap(item => item.concurrency.map(stage => stage.concurrency))).filter(finite);
    const shapes = unique(traffic.flatMap(item => item.matrix.map(point => `${point.isl ?? '—'} / ${point.osl ?? '—'}`)));
    const rates = unique(traffic.flatMap(item => item.rates.map(stage => stage.rate))).filter(finite);
    const rateStageCounts = unique(traffic.map(item => item.rates.length));
    const succeeded = cases.filter(item => item.status === 'succeeded').length;
    const failed = cases.filter(item => ['failed', 'cancelled'].includes(item.status)).length;
    const finished = succeeded + failed;
    // Show the range of independently measured stages/points, never a pooled
    // percentile or the last case summary masquerading as the whole sweep.
    const results = (workflow ? cases : [task]).flatMap(recordedResults).filter(({ metrics }) => metrics && (finite(metrics.throughput_tps) || finite(metrics.latency_distributions?.ttft?.p95_ms) || finite(metrics.success_rate)));
    const summaries = results.map(result => result.metrics);
    const scopes = unique(results.map(result => result.scope));
    const resultLabel = scopes.length === 1 ? scopes[0] : scopes.length ? 'result' : 'summary';
    const outputValues = summaries.map(metrics => metrics.throughput_tps).filter(finite);
    const ttftValues = summaries.map(metrics => metrics.latency_distributions?.ttft?.p95_ms).filter(finite);
    const successValues = summaries.map(metrics => metrics.success_rate).filter(finite);
    const started = Date.parse(task.started_at || task.created_at);
    const ended = Date.parse(task.finished_at);
    const seconds = Number.isFinite(started) && Number.isFinite(ended) && ended >= started ? Math.round((ended - started) / 1000) : null;
    const elapsed = seconds === null ? '—' : seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m${seconds % 60 ? ` ${seconds % 60}s` : ''}` : `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
    const executionIds = new Set([task.deployment_execution_id, ...cases.map(item => item.execution_id)].filter(Boolean));
    const endpoints = (task.readyDeployments || []).filter(item => executionIds.has(item.execution_id));
    const configCount = Object.keys(task.configuration_artifacts || {}).length || unique(cases.map(item => item.configuration_artifact_id)).length;
    const isActive = active.has(task.status);
    const statusStyle = task.status === 'succeeded' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300' : ['failed', 'cancelled'].includes(task.status) ? 'border-rose-500/30 bg-rose-500/10 text-rose-300' : isActive ? 'border-cyan-500/30 bg-cyan-500/10 text-cyan-300' : 'border-slate-700 text-slate-400';
    const StatusIcon = task.status === 'succeeded' ? CheckCircle2 : isActive ? Activity : CircleDashed;
    const metricTiles = [
        { label: 'Output tokens/s', value: range(outputValues), coverage: outputValues.length },
        { label: 'TTFT P95 · ms', value: range(ttftValues), coverage: ttftValues.length },
        { label: 'Success · %', value: range(successValues), coverage: successValues.length },
        { label: 'Elapsed', value: elapsed, coverage: null },
    ];
    const title = task.name || (workflow ? modelName(task.model) || 'Evaluation' : `${models[0] || 'Model not recorded'} · Endpoint benchmark`);
    return <article aria-label={title} onClick={event => { if (onOpen && !event.target.closest('button, details')) onOpen(); }} className={`overflow-hidden rounded-xl border bg-[#0c1422] transition-colors ${workflow ? 'cursor-pointer' : ''} ${isActive ? 'border-cyan-500/35' : 'border-slate-800 hover:border-slate-700'}`}>
        <div className="flex flex-col gap-4 p-4 xl:flex-row xl:items-center">
            <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2.5"><span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border ${statusStyle}`}><StatusIcon size={17} className={isActive ? 'animate-pulse' : ''} /></span><div className="min-w-0 flex-1"><h3 className="truncate text-sm font-semibold text-slate-100">{workflow ? <button type="button" onClick={onOpen} title={title} className="max-w-full truncate text-left hover:text-cyan-200 focus-visible:outline focus-visible:outline-cyan-400">{title}</button> : title}</h3><p className="truncate text-xs text-slate-400" title={models.join(', ')}>{models.join(', ') || 'Model not recorded'}</p></div><span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium ${statusStyle}`}>{task.status || 'unknown'}</span></div>
                <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[10px]">
                    {(guideNames.length ? guideNames : [workflow ? 'Guide not recorded' : 'Existing endpoint']).map(guide => <span key={guide} className="rounded-md border border-violet-500/20 bg-violet-500/[0.07] px-2 py-1 text-violet-200">{guide}</span>)}
                    {workloads.length > 0 && <span className="max-w-52 truncate rounded-md border border-slate-800 px-2 py-1 text-slate-400" title={workloads.join(', ')}>{workloads[0]}{workloads.length > 1 ? ` +${workloads.length - 1}` : ''}</span>}
                    {shapes.length > 0 && <span className="px-1 text-slate-500" title={`${shapes.join(', ')} input / output tokens`}>{shapes.length === 1 ? `ISL / OSL ${shapes[0]}` : `${shapes.length} token shapes`}</span>}
                    {concurrency.length > 0 && <span className="px-1 text-cyan-300/75" title="Configured closed-loop concurrency">C {range(concurrency)}</span>}
                    {rates.length > 0 && <span className="px-1 text-cyan-300/75" title="Configured open-loop arrival rates; not traffic-worker count">Offered {range(rates)} req/s · {range(rateStageCounts)} rate stages</span>}
                </div>
                <div className="mt-3">
                    {progress.caseLabel && <p className="mb-1 text-[10px] font-medium tabular-nums text-cyan-300" title={progress.currentLabel}>{progress.caseLabel}</p>}
                    <div role="progressbar" aria-label={[progress.caseLabel, progress.message].filter(Boolean).join(' · ')} aria-valuetext={progress.message} className="flex gap-1.5">
                        {progress.segments.map(segment => <div key={segment.id} className="min-w-0 flex-1" title={`${segment.label}: ${segment.state}`}>
                            <div className={`h-1.5 overflow-hidden rounded-full ${segment.state === 'complete' ? 'bg-emerald-400/80' : segment.state === 'failed' ? 'bg-rose-400' : segment.state === 'cancelled' ? 'bg-amber-400' : 'bg-slate-800'}`}>
                                {segment.state === 'active' && <span className="benchmark-progress block h-full w-1/3 rounded-full bg-cyan-400" />}
                            </div>
                            <p className={`mt-1 text-[9px] ${segment.state === 'failed' ? 'text-rose-300' : segment.state === 'active' ? 'text-cyan-300' : segment.state === 'complete' ? 'text-emerald-300' : 'text-slate-500'}`}>{segment.label}{segment.state === 'failed' ? ' · Failed' : segment.state === 'cancelled' ? ' · Stopped' : ''}</p>
                        </div>)}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center justify-between gap-1 text-[10px] text-slate-400">
                        <span>{progress.message}</span>
                        {workflow && cases.length > 0 && <span>{finished} / {cases.length} cases{failed > 0 ? ` · ${failed} failed/cancelled` : ''}</span>}
                    </div>
                </div>

            </div>
            <div className="xl:w-[390px] xl:shrink-0"><div className="grid grid-cols-4 gap-px overflow-hidden rounded-lg border border-slate-800 bg-slate-800">{metricTiles.map(tile => <div key={tile.label} className="min-w-0 bg-[#0a101c] px-2.5 py-3" title={tile.coverage === null ? 'Wall time from recorded start/creation to finish; includes deployment.' : `${tile.coverage} recorded ${resultLabel} values; no pooled percentile or task aggregate.`}><p className="truncate text-[9px] text-slate-500">{tile.label}</p><p className={`mt-1.5 truncate font-mono text-sm font-semibold tabular-nums ${tile.value === '—' ? 'text-slate-600' : 'text-slate-100'}`}>{tile.value}</p></div>)}</div><p className="mt-1.5 text-right text-[9px] text-slate-600">{`Saved ${resultLabel}${summaries.length > 1 ? ' range' : ''}`} · {summaries.length} recorded{workflow && cases.length > 0 ? ` / ${cases.length} cases` : ''}</p></div>
            <div className="flex shrink-0 items-center gap-1.5">{workflow && <button type="button" onClick={onOpen} aria-label="Open results" className="inline-flex h-8 items-center gap-1 rounded-lg border border-cyan-500/25 bg-cyan-500/5 px-2.5 text-[10px] text-cyan-200 hover:bg-cyan-500/10">Results<ArrowUpRight size={13} /></button>}{workflow && ['failed', 'cancelled'].includes(task.status) && <button type="button" aria-label="Retry task" title="Retry" onClick={() => onAction(task, 'retry')} className="rounded-lg border border-slate-700 p-2 text-slate-400 hover:text-emerald-300"><RotateCcw size={13} /></button>}{!terminal.has(task.status) && <button type="button" aria-label="Cancel task" title={task.deployment_ownership === 'evaluation' || (workflow && task.deployment_ownership !== 'existing-endpoint') ? "Cancel benchmark and Evaluation-created deployments" : "Cancel benchmark only; existing endpoint stays running"} onClick={() => onAction(task, 'cancel')} className="rounded-lg border border-slate-700 p-2 text-slate-400 hover:text-amber-300"><Square size={13} /></button>}{terminal.has(task.status) && <button type="button" aria-label="Delete task" title="Delete" onClick={() => onAction(task, 'delete')} className="rounded-lg border border-slate-700 p-2 text-slate-400 hover:text-rose-300"><Trash2 size={13} /></button>}</div>
        </div>
        {progress.error && <p className="mx-4 mb-3 truncate rounded-md bg-rose-500/5 px-2 py-1.5 text-[11px] text-rose-300" title={String(progress.error)}>{String(progress.error)}</p>}
        {task.previousBenchmarkFailures?.length > 0 && <details className="mx-4 mb-3 rounded-md bg-slate-800/30 px-2 py-1.5 text-[11px] text-slate-400">
            <summary className="cursor-pointer">Benchmark attempt history · {task.previousBenchmarkFailures.length} previous interrupted or failed attempt(s)</summary>
            <p className="mt-2">These attempts ended before the current attempt. The task status above reflects the current attempt.</p>
            {task.previousBenchmarkFailures.map(run => <p key={run.id} className="mt-2 break-words">{run.error === 'evaluation process was interrupted by a service restart' ? 'Previous benchmark interrupted by an evaluation service restart.' : `Previous benchmark attempt failed: ${run.error || 'Failure reason not recorded'}`}</p>)}
        </details>}
        <details className="border-t border-slate-800/70 px-4 py-2 text-[10px] text-slate-500"><summary className="cursor-pointer select-none hover:text-slate-300">Task details{endpoints.length ? ` · ${endpoints.length} ready endpoint${endpoints.length > 1 ? 's' : ''}` : ''}</summary><div className="mt-3 grid gap-x-6 gap-y-2 pb-2 sm:grid-cols-2 lg:grid-cols-3"><p className="flex items-center gap-1.5"><Layers3 size={12} />{configCount || '—'} configurations · {workflow ? 'Deploy + benchmark' : 'Existing endpoint'}</p><p className="flex items-center gap-1.5"><Clock3 size={12} />Created {date(task.created_at)}</p><p>Runner: {task.harness || benchmarks[0]?.harness || '—'} · traffic workers: {task.parallelism ?? benchmarks[0]?.parallelism ?? '—'}</p><p className="break-all">ID: {task.id}</p><p className="break-all">Topology: {unique(configurationFacts.map(item => item.topology)).join(' / ') || '—'}</p><p className="break-all">Workloads: {workloads.join(', ') || '—'}</p>{endpoints.map(endpoint => <p key={endpoint.execution_id} className="break-all font-mono text-cyan-300 sm:col-span-2">{endpoint.model} · {endpoint.forwarded_endpoint || endpoint.endpoint}</p>)}</div></details>
    </article>;
}
