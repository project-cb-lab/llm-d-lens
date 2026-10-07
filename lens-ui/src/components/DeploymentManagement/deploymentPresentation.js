// Presentation mapping for deployment records returned by
// GET /api/v1/deployments/executions. Kept free of JSX and network code so the
// same rules can be reused by the table, the detail modal and the stat cards.

export const STATUS_GROUPS = {
    ready: 'Ready',
    succeeded: 'Ready',
    partially_succeeded: 'Ready',

    deploying: 'In progress',
    rendered: 'In progress',
    validated: 'In progress',
    draft: 'In progress',
    rolling_back: 'In progress',
    queued: 'In progress',
    rendering: 'In progress',
    running: 'In progress',
    cancelling: 'In progress',

    failed: 'Failed',

    cleaned: 'Cleaned',
    cleaned_up: 'Cleaned',
    rolled_back: 'Cleaned',
    stopped: 'Cleaned',
    cancelled: 'Cleaned',
};

const STATUS_TONES = {
    Ready: 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200',
    'In progress': 'border-sky-400/30 bg-sky-400/10 text-sky-200',
    Failed: 'border-rose-400/30 bg-rose-400/10 text-rose-200',
    Cleaned: 'border-slate-400/30 bg-slate-400/10 text-slate-400',
    Unknown: 'border-slate-400/30 bg-slate-400/10 text-slate-400',
};

// Pulsing status-dot colors, matching the dot used on the Clusters page.
const STATUS_DOT_CLASSES = {
    Ready: 'bg-emerald-400',
    'In progress': 'bg-sky-400',
    Failed: 'bg-red-400',
    Cleaned: 'bg-slate-500',
    Unknown: 'bg-slate-500',
};

// Statuses that still have an in-flight lifecycle transition.
const IN_PROGRESS_STATUSES = new Set(['deploying', 'rolling_back']);

export const STATUS_FILTER_OPTIONS = Object.keys(STATUS_GROUPS);

export function statusGroup(status) {
    return STATUS_GROUPS[status] || 'Unknown';
}

export function statusTone(status) {
    return STATUS_TONES[statusGroup(status)] || STATUS_TONES.Unknown;
}

export function statusLabel(status) {
    if (!status) return 'UNKNOWN';
    return String(status).replace(/_/g, ' ').toUpperCase();
}

export function statusDotClass(status) {
    return STATUS_DOT_CLASSES[statusGroup(status)] || STATUS_DOT_CLASSES.Unknown;
}

export function statusDotClassForGroup(group) {
    return STATUS_DOT_CLASSES[group] || STATUS_DOT_CLASSES.Unknown;
}

export function statusToneForGroup(group) {
    return STATUS_TONES[group] || STATUS_TONES.Unknown;
}

// Container/pod status reasons that indicate the pod is unhealthy regardless
// of restart count, since they don't happen as part of a normal startup
// retry loop (unlike CrashLoopBackOff/Error, which many init containers hit
// intentionally while polling for a dependency to become ready).
const POD_HARD_FAILURE_REASONS = new Set([
    'ImagePullBackOff',
    'ErrImagePull',
    'InvalidImageName',
    'CreateContainerConfigError',
    'CreateContainerError',
    'RunContainerError',
    'OOMKilled',
    'Evicted',
]);

// Reasons that are also failure signals, but only once they've actually
// looped a few times -- a fresh pod can legitimately show these for a bit
// while a slow-loading model or a wait-for-dependency init container retries.
const POD_RETRYABLE_FAILURE_REASONS = new Set(['CrashLoopBackOff', 'Error']);
const RETRYABLE_FAILURE_RESTART_THRESHOLD = 3;

function isFailingPod(pod) {
    if (!pod) return false;
    if (pod.phase === 'Failed') return true;
    if (POD_HARD_FAILURE_REASONS.has(pod.status_reason)) return true;
    if (POD_RETRYABLE_FAILURE_REASONS.has(pod.status_reason)) {
        return (pod.restarts || 0) >= RETRYABLE_FAILURE_RESTART_THRESHOLD;
    }
    return false;
}

// Derive a status group purely from live pod data, independent of the
// deployment's own (potentially stale) `status` field. Returns null when the
// pod list can't tell us anything conclusive (not yet fetched, or empty while
// the deployment is still being created).
export function derivePodStatusGroup(pods) {
    if (!Array.isArray(pods) || pods.length === 0) return null;
    // Completed pods (for example the precise-routing calibration Job) are
    // terminal successes, not part of the deployment's running workload, so they
    // must never read as "not ready".
    const livePods = pods.filter((pod) => pod && pod.phase !== 'Succeeded');
    if (livePods.length === 0) return null;
    if (livePods.some(isFailingPod)) return 'Failed';
    if (livePods.some((pod) => !pod.ready)) return 'In progress';
    return 'Ready';
}

// Combine the deployment's own status with what its pods currently report.
// A pod-level failure always wins (surfaces crashes even if the deployment
// record still says "ready"). Pods that are merely not-ready-yet downgrade a
// "ready" deployment to "in progress" instead of silently staying green.
// Terminal states (Cleaned) are never overridden by transient pod snapshots.
export function effectiveStatusGroup(item, pods) {
    const backendGroup = statusGroup(item?.status);
    const podGroup = derivePodStatusGroup(pods);
    if (!podGroup) return backendGroup;
    if (backendGroup === 'Cleaned') return backendGroup;
    if (podGroup === 'Failed') return 'Failed';
    if (podGroup === 'In progress' && backendGroup === 'Ready') return 'In progress';
    return backendGroup;
}

export function effectiveStatusLabel(item, pods) {
    const backendGroup = statusGroup(item?.status);
    const effective = effectiveStatusGroup(item, pods);
    if (effective === backendGroup) return statusLabel(item?.status);
    if (effective === 'Failed') return `${statusLabel(item?.status)} · POD ERROR`;
    if (effective === 'In progress') return `${statusLabel(item?.status)} · POD NOT READY`;
    return statusLabel(item?.status);
}

export function deploymentTitle(item) {
    return item?.display_name || item?.name || item?.model || item?.execution_id || 'Deployment';
}

export function deploymentKind(item) {
    const parts = [item?.backend, item?.guide].filter(Boolean);
    return parts.length ? parts.join(' · ') : '—';
}

export function deploymentEndpoint(item) {
    return item?.forwarded_endpoint || item?.endpoint || '';
}

export function deploymentInternalEndpoint(item) {
    return item?.endpoint || '';
}

export function deploymentLocalEndpoint(item) {
    return item?.forwarded_endpoint || '';
}

export { formatTimestamp } from '../../utils/formatTimestamp';

export function shortId(executionId) {
    return String(executionId || '').slice(-8);
}

// Text the user must retype in the delete dialog. Display name when present so
// the confirmation matches what the row shows, otherwise the id suffix.
export function deleteConfirmationPhrase(item) {
    return item?.display_name?.trim() || shortId(item?.execution_id);
}

// The backend cancels an in-flight deployment and cleans up its cluster
// resources before deleting the record, so deletion is never blocked here.
export function deletionBlockedReason() {
    return '';
}

// True while the deployment is still deploying/rolling back, so the UI can
// warn the user that deleting it now will cancel the in-flight operation.
export function isDeletionInProgressStatus(item) {
    return IN_PROGRESS_STATUSES.has(item?.status);
}

export function summarizeStatuses(items) {
    const summary = { total: items.length, Ready: 0, 'In progress': 0, Failed: 0, Cleaned: 0 };
    for (const item of items) {
        const group = statusGroup(item?.status);
        if (group in summary) {
            summary[group] += 1;
        } else {
            summary.Cleaned += 1;
        }
    }
    return summary;
}

export function clusterOptions(items) {
    return [...new Set(items.map((item) => item?.cluster_id).filter(Boolean))].sort();
}

export { errorMessage } from '../../utils/errorMessage';
