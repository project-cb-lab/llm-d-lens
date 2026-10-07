export function evaluationTasks(workflows, benchmarks, readyDeployments = []) {
    const childIds = new Set(workflows.flatMap(workflow => [
        workflow.evaluation_run_id,
        ...(workflow.cases || []).map(item => item.evaluation_run_id),
    ]).filter(Boolean));
    // Ownership survives retries, which replace the case's current benchmark ID.
    // A workflow-owned benchmark remains a child even if its parent is not loaded.
    const standalone = benchmarks.filter(run => !run.evaluation_workflow_id && !childIds.has(run.id));
    return [...workflows, ...standalone]
        .map(task => ({
            ...task,
            readyDeployments,
            previousBenchmarkFailures: task.kind === 'workflow' ? benchmarks.filter(run =>
                run.evaluation_workflow_id === task.id && !childIds.has(run.id) && run.status === 'failed') : [],
        }))
        .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
}
