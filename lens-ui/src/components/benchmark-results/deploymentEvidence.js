const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};

export function deploymentEvidence(item = {}) {
    const record = item.evaluationCase || {};
    const embedded = record.deployment_configuration?.content;
    let parsed = embedded;
    if (typeof embedded === 'string') { try { parsed = JSON.parse(embedded); } catch { parsed = null; } }
    const config = { ...object(record.spec), ...object(record.configuration), ...object(parsed) };
    const observation = record.metrics?.observability || {};
    const snapshot = item.resource_snapshot || record.resource_snapshot;
    return {
        config: embedded || config,
        model: typeof config.model === 'string' ? config.model : config.model?.name,
        runtime: config.model_server || config.runtime?.image || (typeof config.runtime === 'string' ? config.runtime : null),
        replicas: config.decode?.replicaCount ?? config.decode_replicas ?? config.replicas,
        tp: config.decode?.tensorParallelSize ?? config.decode_tensor_parallel_size ?? config.tensor_parallel_size,
        accelerator: config.accelerator || snapshot?.accelerator,
        gpus: snapshot?.requested_gpus,
        podCount: Array.isArray(snapshot?.pods) ? snapshot.pods.length : null,
        telemetry: observation.status === 'available' ? 'Samples recorded' : observation.status === 'empty' ? 'No samples' : 'Unavailable',
        telemetryReason: observation.reason || (observation.status === 'available' ? 'Saved benchmark window · not current monitoring status' : 'No monitoring samples were recorded for this benchmark.'),
        lifecycle: item.execution_status || item.deploymentStatus || item.status || 'Not deployed',
    };
}

export function liveDeployments(cases = []) {
    const seen = new Set();
    return cases.filter(item => {
        if (!item.execution_id || seen.has(item.execution_id)) return false;
        seen.add(item.execution_id);
        return true;
    }).map(item => ({ ...item, liveAvailable: (item.execution_status || item.deploymentStatus || item.status) === 'ready' }));
}
