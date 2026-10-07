const stageForStatus = { queued: 'queued', rendering: 'deploying', deploying: 'deploying', benchmarking: 'benchmarking', succeeded: 'complete' };
const labels = { queued: 'Queued', deploying: 'Deploy', benchmarking: 'Benchmark', complete: 'Complete' };

export function taskProgress(task) {
    const workflow = task.kind === 'workflow';
    const cases = task.cases || [];
    const stopped = ['failed', 'cancelled'].includes(task.status);
    const current = task.status === 'succeeded' ? task :
        (!stopped && cases.find(item => item.id === task.active_case_id)) ||
        (stopped && cases.find(item => item.status === 'failed')) ||
        cases.find(item => ['deploying', 'rendering', 'benchmarking', 'running'].includes(item.status)) ||
        (stopped && cases.find(item => item.status === 'cancelled')) ||
        cases.find(item => item.status === 'queued') || task;
    let stage = stageForStatus[current.status];
    if (stopped) {
        stage = current.failed_stage || task.failed_stage ||
            (['queued', 'deploying', 'rendering', 'benchmarking'].includes(current.status) ? stageForStatus[current.status] : null) ||
            (current.evaluation_run_id || current.execution_id ? 'benchmarking' :
                current.deployment_run_id ? 'deploying' :
                    !workflow && current.started_at ? 'benchmarking' : null);
    } else if (!stage) {
        stage = !workflow || current.evaluation_run_id ? 'benchmarking' : current.deployment_run_id ? 'deploying' : null;
    }
    stage = stageForStatus[stage] || stage;
    const ids = workflow ? ['queued', 'deploying', 'benchmarking', 'complete'] : ['queued', 'benchmarking', 'complete'];
    const index = ids.indexOf(stage);
    const segments = ids.map((id, i) => ({ id, label: labels[id], state:
        index < 0 ? 'unknown' : task.status === 'succeeded' || i < index ? 'complete' :
            i > index ? 'pending' : stopped ? task.status : 'active',
    }));
    const stateLabel = task.status === 'failed' ? 'Failed at' : task.status === 'cancelled' ? 'Stopped at' : 'Current stage';
    const message = task.status === 'succeeded' ? 'Completed' : index < 0 ?
        (stopped ? 'Stopped stage not recorded' : 'Waiting for stage information') : `${stateLabel}: ${labels[stage]}`;
    const caseIndex = cases.indexOf(current);
    return { segments, message, caseLabel: cases.length > 1 && caseIndex >= 0 ? `Case ${caseIndex + 1}/${cases.length}` : null,
        currentLabel: caseIndex >= 0 ? `Case ${caseIndex + 1}${current.scenario_name ? ` · ${current.scenario_name}` : ''}` : null,
        error: current.error || task.error, active: !stopped && task.status !== 'succeeded' && index >= 0 };
}
