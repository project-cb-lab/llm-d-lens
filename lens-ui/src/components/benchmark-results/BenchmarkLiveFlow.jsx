import React, { useEffect, useRef, useState } from 'react';
import { Activity, RefreshCw } from 'lucide-react';
import { DeploymentProfilingPanel, FlowMap } from '../ClusterMonitoringStack/DeploymentProfilingPanel.jsx';
import { getDeploymentMonitoring, manageDeploymentMonitoring } from '../ClusterMonitoringStack/clusterMonitoringStackBackend.js';
import { historicalFlows } from './historicalFlow.js';
import { shortCase } from './linkedExperiment.js';
import { liveDeployments } from './deploymentEvidence.js';

function DeploymentFlow({ deployment }) {
    const [monitoring, setMonitoring] = useState(null);
    const [error, setError] = useState('');
    const [enabling, setEnabling] = useState(false);
    const [revision, setRevision] = useState(0);
    const mounted = useRef(false);
    const executionId = deployment.execution_id;
    useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
    useEffect(() => {
        const controller = new AbortController();
        let timer;
        async function load() {
            try {
                const value = await getDeploymentMonitoring(executionId, { signal: controller.signal });
                if (!controller.signal.aborted) { setMonitoring(value); setError(''); }
            } catch (nextError) {
                if (!controller.signal.aborted) setError(nextError.message);
            }
            if (!controller.signal.aborted) timer = setTimeout(load, 10000);
        }
        load();
        return () => { controller.abort(); clearTimeout(timer); };
    }, [executionId, revision]);
    async function enable() {
        setEnabling(true); setError('');
        try {
            const value = await manageDeploymentMonitoring(executionId, 'enable');
            if (mounted.current) { setMonitoring(value); setRevision(value => value + 1); }
        } catch (nextError) { if (mounted.current) setError(nextError.message); }
        finally { if (mounted.current) setEnabling(false); }
    }
    const enabled = monitoring?.enabled === true;
    const state = enabling ? 'Enabling monitoring' : error ? 'Monitoring check failed' : !monitoring ? 'Checking monitoring' : enabled ? 'Monitoring enabled' : monitoring.status === 'disabled' ? 'Monitoring disabled' : 'Monitoring unavailable';
    return <div className="space-y-4">
        <div className="rounded-xl border border-slate-800 bg-[#0b1423] p-4"><div className="flex flex-wrap items-center justify-between gap-3"><p role="status" className={`flex items-center gap-2 text-xs ${enabled && !error ? 'text-emerald-300' : 'text-amber-200'}`}><Activity size={15} />{state}</p><div className="flex gap-2"><button type="button" onClick={() => setRevision(value => value + 1)} className="rounded-lg border border-slate-700 p-2 text-slate-300" aria-label="Refresh monitoring status"><RefreshCw size={14} /></button>{!enabled && <button type="button" onClick={enable} disabled={enabling || !monitoring} className="rounded-lg border border-sky-400/30 px-3 py-2 text-xs text-sky-200 disabled:opacity-50">{enabling ? 'Enabling…' : 'Enable monitoring / retry'}</button>}</div></div>
            {!enabled && monitoring?.message && <p className="mt-3 text-xs text-slate-400">{monitoring.message}</p>}
            {monitoring?.stack_ready === false && <p className="mt-2 text-xs text-amber-200">The cluster monitoring stack is not ready. Enable it in Cluster observability before retrying deployment monitoring.</p>}
            {error && <p role="alert" className="mt-3 text-xs text-rose-300">{error}</p>}
        </div>
        {enabled && !enabling && <DeploymentProfilingPanel deployment={deployment} refreshMs={5000} />}
    </div>;
}

export default function BenchmarkLiveFlow({ cases = [], initialExecutionId, details }) {
    const choices = liveDeployments(cases).filter(item => item.liveAvailable);
    const history = historicalFlows(details);
    const [mode, setMode] = useState('live');
    const [historyId, setHistoryId] = useState('');
    const [metric, setMetric] = useState('request');
    const recorded = history.find(item => item.run.caseId === historyId) || history[0];
    const showHistory = history.length > 0 && (mode === 'history' || !choices.length);
    const [selectedId, setSelectedId] = useState(initialExecutionId || '');
    const selected = choices.find(item => item.execution_id === selectedId) || choices.find(item => item.usedByEvaluation && item.liveAvailable) || choices.find(item => item.liveAvailable) || choices[0];
    if (!selected && !recorded) return null;
    const modeControl = choices.length > 0 && history.length > 0 && <div role="group" aria-label="Monitoring source" className="flex gap-2">{[['live','Live'],['history','Recorded']].map(([value,label]) => <button type="button" key={value} aria-pressed={mode === value} onClick={() => setMode(value)} className="rounded border border-slate-700 px-3 py-2 text-xs text-cyan-200">{label}</button>)}</div>;
    if (showHistory) return <section aria-label="Recorded deployment flow" className="space-y-4">
        {modeControl}
        <header className="flex flex-wrap items-end justify-between gap-3 rounded-xl border border-slate-800 bg-[#0b1423] p-5">
            <div><h2 className="text-sm font-semibold">Recorded deployment flow</h2><p className="mt-2 text-xs text-slate-400">Benchmark window averages · {recorded.data.window.start} → {recorded.data.window.end}</p></div>
            <label className="text-xs text-slate-400">Configuration<select aria-label="Recorded flow configuration" value={recorded.run.caseId} onChange={event => setHistoryId(event.target.value)} className="ml-2 rounded border border-slate-700 bg-slate-950 p-2">{history.map(item => <option key={item.run.caseId} value={item.run.caseId}>{shortCase(item.run)}</option>)}</select></label>
            <label className="text-xs text-slate-400">Flow<select aria-label="Recorded flow metric" value={metric} onChange={event => setMetric(event.target.value)} className="ml-2 rounded border border-slate-700 bg-slate-950 p-2"><option value="request">Requests</option><option value="token">Tokens</option></select></label>
        </header>
        <FlowMap key={recorded.run.caseId} data={recorded.data} metric={metric} historical/>
    </section>;
    return <section className="space-y-4">
        {modeControl}
        <header className="flex flex-wrap items-end justify-between gap-4 rounded-xl border border-slate-800 bg-[#0b1423] p-5"><div><h2 className="flex items-center gap-2 text-sm font-semibold"><Activity size={17} className="text-emerald-300" />Live deployment flow</h2><p className="mt-2 max-w-3xl text-xs leading-5 text-slate-400">Current traffic · updates every 5s · all requests to this deployment.</p></div>{choices.length > 0 && <label className="grid min-w-0 gap-2 text-[11px] text-slate-400">Deployment execution<select aria-label="Live flow deployment" value={selected?.execution_id || ''} onChange={event => setSelectedId(event.target.value)} className="max-w-full rounded-lg border border-slate-700 bg-slate-950 p-2 text-xs text-slate-200">{choices.map(item => <option key={item.execution_id} value={item.execution_id}>{item.guide || item.id} · Attempt {item.attempt || 1} · {item.execution_status || item.deploymentStatus || item.status}</option>)}</select></label>}</header>
        <DeploymentFlow key={selected.execution_id} deployment={selected} />
    </section>;
}
