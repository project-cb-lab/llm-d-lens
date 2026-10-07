import { Select } from '../ui/FormControls';
import ResultExports from './ResultExports.jsx';
import { overviewCases } from './experimentOverview.js';
import { shortCase } from './linkedExperiment.js';
import BenchmarkOutcomes from './BenchmarkOutcomes.jsx';

const valueText = value => value == null || value === '' ? 'Not recorded' : typeof value === 'object' ? JSON.stringify(value) : String(value);

export default function BenchmarkScenario({ details, runs, selected, onCaseSelect }) {
    const cases = [...new Map(runs.map(run => [run.caseId, run])).values()];
    if (!selected) return null;
    const summary = overviewCases(details).find(row => row.id === selected.caseId);
    const config = selected.configuration || {};
    const benchmark = config.benchmark || {};
    const serving = config.decode || config.serving || {};
    const infra = config.infrastructure || {};
    const groups = [
        ['Infra layer', [
            ['Provider / machine type', [infra.provider || config.provider, infra.machineType || config.machineType].filter(Boolean).join(' / ')],
            ['Accelerator', summary?.hardware],
            ['Replicas / parallelism', summary?.topology],
            ['Allocated accelerators', summary?.gpus],
        ]],
        ['Model serving layer', [
            ['Model name', typeof config.model === 'string' ? config.model : config.model?.name],
            ['Runtime image', config.runtime?.image || config.image],
            ['Routing / optimization', `${shortCase(selected)} · ${config.routerProfile || selected.guide}`],
            ['Context / max sequences', `${valueText(serving.maxModelLen)} / ${valueText(serving.maxNumSeqs)}`],
        ]],
        ['Workload', [
            ['Test harness', benchmark.harness || 'inference-perf'],
            ['Input / output tokens', `${valueText(selected.isl)} / ${valueText(selected.osl)}`],
            ['Selected load', selected.load == null ? 'Full run' : `${selected.load} ${selected.loadKind === 'rate' ? 'req/s' : 'concurrent requests'}`],
            ['Workload source', benchmark.workload || (benchmark.workload_yaml ? 'Custom YAML' : benchmark.shared_prefix ? 'Shared prefixes' : benchmark.matrix ? 'Fixed input / output lengths' : null)],
        ]],
    ];
    return <div className="space-y-4">
        <section aria-label="Benchmark scenario" className="rounded-2xl border border-slate-800 bg-[#0b1324] p-5 lg:p-6">
            <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
                <div className="flex min-w-0 flex-wrap items-center gap-3"><h2 className="text-sm font-bold uppercase tracking-wider text-emerald-400">Benchmark scenario</h2>{cases.length > 1 ? <Select aria-label="Scenario" value={selected.caseId} onChange={event => onCaseSelect(event.target.value)} className="w-auto max-w-full text-xs">{cases.map(run => <option key={run.caseId} value={run.caseId}>{shortCase(run)}</option>)}</Select> : <span className="text-xs text-slate-300">{shortCase(selected)}</span>}</div>
                <ResultExports details={details} run={selected}/>
            </div>
            <div className="grid gap-6 md:grid-cols-3 md:divide-x md:divide-slate-800">{groups.map(([title, fields]) => <div key={title} className="min-w-0 md:pl-6 md:first:pl-0"><h3 className="mb-4 text-xs font-semibold uppercase tracking-wide text-slate-400">{title}</h3><dl className="space-y-4">{fields.map(([label, value]) => <div key={label}><dt className="text-xs text-slate-500">{label}</dt><dd className="mt-1 break-words font-mono text-xs font-semibold leading-5 text-slate-100">{valueText(value)}</dd></div>)}</dl></div>)}</div>
        </section>
        <div className="min-w-0 rounded-2xl border border-slate-800 bg-[#0f1829] p-5"><h2 className="text-sm font-bold uppercase tracking-wider text-emerald-400">Primary outcomes</h2><BenchmarkOutcomes runs={runs} selected={selected}/></div>
    </div>;
}
