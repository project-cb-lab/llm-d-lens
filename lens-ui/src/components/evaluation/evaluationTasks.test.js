import assert from 'node:assert/strict';
import test from 'node:test';
import { evaluationTasks } from './evaluationTasks.js';

test('workflow benchmarks, including prior attempts and legacy links, stay under their task', () => {
    const workflows = [
        { id: 'task', kind: 'workflow', cases: [{ evaluation_run_id: 'current' }] },
        { id: 'legacy', kind: 'workflow', evaluation_run_id: 'legacy-child' },
    ];
    const benchmarks = [
        { id: 'current' },
        { id: 'old', status: 'failed', evaluation_workflow_id: 'task' },
        { id: 'legacy-child' },
        { id: 'unloaded-parent', evaluation_workflow_id: 'unloaded' },
        { id: 'standalone', deployment_execution_id: 'shared-endpoint' },
    ];
    assert.deepEqual(evaluationTasks(workflows, benchmarks).map(task => task.id), ['task', 'legacy', 'standalone']);
    assert.deepEqual(evaluationTasks(workflows, benchmarks)[0].previousBenchmarkFailures.map(run => run.id), ['old']);
});
