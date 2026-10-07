// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import { scaleBytes } from '../../utils/formatBytes';

export const FALLBACK_SCENARIOS = [
    { value: 'chat', label: 'Chat' },
    { value: 'api-calling', label: 'Tool & API Use' },
    { value: 'coding', label: 'Code Generation' },
];

export const RESPONSE_CODE_ISSUE_PAGE_SIZE = 8;

export const SCENARIO_LABELS = Object.fromEntries(
    FALLBACK_SCENARIOS.map((scenario) => [scenario.value, scenario.label]),
);

export const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'canceled', 'stopped']);

export const RUNNING_STATUSES = new Set(['queued', 'pending', 'running', 'stopping', 'cancelling']);

export const STOPPABLE_STATUSES = new Set(['queued', 'pending', 'running']);

export function arrayFrom(payload, keys) {
    if (Array.isArray(payload)) return payload;
    for (const key of keys) {
        if (Array.isArray(payload?.[key])) return payload[key];
    }
    return [];
}

export function scenarioOptions(payload) {
    const items = arrayFrom(payload, ['scenarios', 'items', 'data']);
    if (!items.length) return FALLBACK_SCENARIOS;
    return items.map((item) => {
        if (typeof item === 'string') return { value: item, label: scenarioDisplayName(item) };
        const value = item.value || item.id || item.name || item.slug || item.label;
        return {
            ...item,
            value,
            label: SCENARIO_LABELS[value] || item.label || item.display_name || item.name || value,
        };
    }).filter((item) => item.value);
}

export function unwrapTask(payload) {
    return payload?.task || payload?.data?.task || payload?.data || payload;
}

export function numberAt(source, paths) {
    for (const path of paths) {
        const value = path.split('.').reduce((current, key) => current?.[key], source);
        if (value !== undefined && value !== null && value !== '') {
            const numeric = Number(value);
            if (Number.isFinite(numeric)) return numeric;
        }
    }
    return null;
}

export function formatNumber(value, suffix = '') {
    if (value === null || value === undefined) return '—';
    const formatted = Math.abs(value) >= 1000
        ? new Intl.NumberFormat(undefined, { maximumFractionDigits: 1, notation: 'compact' }).format(value)
        : new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value);
    return `${formatted}${suffix}`;
}

export function formatMilliseconds(value) {
    if (value === null || value === undefined) return '—';
    return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value)} ms`;
}

export function formatCount(value) {
    if (value === null || value === undefined) return '—';
    return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value);
}

export function formatBytes(value) {
    if (!Number.isFinite(Number(value))) return null;
    const { value: amount, unit, unitIndex } = scaleBytes(value, 3);
    return `${unitIndex === 0 ? amount : amount.toFixed(1)} ${unit}`;
}

export function displayName(value) {
    return String(value || '')
        .replace(/[_-]+/g, ' ')
        .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function taskBackend(task) {
    return task?.simulation?.backend || task?.backend || task?.result?.backend || '—';
}

export function taskDataset(task) {
    const explicit = task?.dataset || task?.prompt?.dataset?.name;
    if (explicit) return explicit;
    const tracePath = task?.prompt?.trace?.path || task?.trace_path;
    return tracePath ? tracePath.split('/').filter(Boolean).pop() : '—';
}

export function taskStatusChip(status) {
    const normalized = String(status || '').toLowerCase();
    if (normalized === 'completed') return 'verified';
    if (normalized === 'running') return 'running';
    if (['queued', 'pending', 'stopping', 'cancelling'].includes(normalized)) return 'pending';
    return normalized || 'inactive';
}

export function taskMetrics(task) {
    const summary = task?.result?.summary || task?.live_summary || task?.summary || {};
    const backendOptions = task?.simulation?.backend_options || task?.backend_options || {};
    let totalRequests = numberAt(summary, ['total_requests', 'requests_total', 'request_count']);
    let failedRequests = numberAt(summary, ['failed_requests', 'requests_failed', 'error_count']);
    if ((totalRequests === null || failedRequests === null) && Array.isArray(summary.completion_timeline)) {
        const totals = summary.completion_timeline.reduce((current, point) => ({
            total: current.total + Number(point.completed_requests || 0),
            failed: current.failed + Number(point.failed_requests || 0),
        }), { total: 0, failed: 0 });
        totalRequests = totals.total;
        failedRequests = totals.failed;
    }
    const ttftSloSeconds = numberAt(backendOptions, ['ttft_slo']);
    const tpotSloSeconds = numberAt(backendOptions, ['tpot_slo']);
    const errorRateSlo = numberAt(backendOptions, ['error_rate_slo']);
    return {
        requestRate: numberAt(summary, ['request_throughput.avg', 'throughput_rps', 'requests_per_second']),
        errorRate: totalRequests > 0 && failedRequests !== null
            ? failedRequests / totalRequests * 100
            : null,
        ttft: numberAt(summary, ['ttft.mean_ms', 'time_to_first_token.avg', 'ttft_ms']),
        tpot: numberAt(summary, ['tpot.mean_ms', 'inter_token_latency.avg', 'tpot_ms']),
        ttftSloMs: ttftSloSeconds === null ? null : ttftSloSeconds * 1000,
        tpotSloMs: tpotSloSeconds === null ? null : tpotSloSeconds * 1000,
        errorRateSlo,
    };
}

export function findNamedSection(source, pattern) {
    if (!source || typeof source !== 'object') return null;
    for (const [key, value] of Object.entries(source)) {
        if (pattern.test(key) && value !== undefined && value !== null) return { [key]: value };
    }
    return null;
}

export function formatDurationSeconds(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    const totalSeconds = Math.round(seconds);
    if (totalSeconds < 60) return `${totalSeconds} s`;
    if (totalSeconds < 3600) {
        const minutes = Math.floor(totalSeconds / 60);
        const remainingSeconds = totalSeconds % 60;
        return remainingSeconds ? `${minutes} min ${remainingSeconds} s` : `${minutes} min`;
    }
    const hours = Math.floor(totalSeconds / 3600);
    const remainingMinutes = Math.floor((totalSeconds % 3600) / 60);
    return remainingMinutes ? `${hours} h ${remainingMinutes} min` : `${hours} h`;
}

export function formatDuration(task, summary, nowMs = Date.now()) {
    const running = RUNNING_STATUSES.has(String(task?.status || '').toLowerCase());
    if (running) {
        const elapsed = String(task?.progress_message || '').match(
            /Simulation running\s*·\s*([\d.]+)s elapsed/
        );
        if (elapsed) return formatDurationSeconds(Number(elapsed[1]));
        // The dashboard clock is anchored to when the backend tool
        // (aiperf/trace-replayer) actually became ready and started running,
        // not to when the task was picked up — pod launch, data staging, and
        // on-demand tool installation shouldn't make the timer appear to be
        // running before there's really anything to measure.
        if (task?.execution_started_at) {
            const start = new Date(task.execution_started_at);
            const elapsedSeconds = (new Date(nowMs).getTime() - start.getTime()) / 1000;
            if (Number.isFinite(elapsedSeconds) && elapsedSeconds >= 0) {
                return formatDurationSeconds(elapsedSeconds);
            }
        }
        return 'Preparing…';
    }
    if (task?.started_at) {
        const start = new Date(task.started_at);
        const end = task.completed_at ? new Date(task.completed_at) : new Date(nowMs);
        const elapsedSeconds = (end.getTime() - start.getTime()) / 1000;
        if (Number.isFinite(elapsedSeconds) && elapsedSeconds >= 0) {
            return formatDurationSeconds(elapsedSeconds);
        }
    }
    const reported = numberAt(summary, ['duration_seconds', 'benchmark_duration.avg', 'duration']);
    return reported === null ? '—' : formatDurationSeconds(reported);
}

export function metricDistribution(source) {
    if (!source || typeof source !== 'object') return [];
    return [
        ['Mean', numberAt(source, ['mean_ms', 'avg_ms', 'mean', 'avg'])],
        ['P50', numberAt(source, ['p50_ms', 'p50'])],
        ['P90', numberAt(source, ['p90_ms', 'p90'])],
        ['P95', numberAt(source, ['p95_ms', 'p95'])],
        ['P99', numberAt(source, ['p99_ms', 'p99'])],
        ['Max', numberAt(source, ['max_ms', 'max'])],
    ].filter(([, value]) => value !== null);
}

export function scenarioDisplayName(value) {
    return SCENARIO_LABELS[value] || displayName(value);
}

export function backendOptionLabel(key) {
    if (key === 'ttft_slo') return 'TTFT SLO';
    if (key === 'tpot_slo') return 'TPOT SLO';
    if (key === 'error_rate_slo') return 'Error Rate SLO';
    return displayName(key);
}

export function backendOptionValue(key, value) {
    if (key === 'ttft_slo' || key === 'tpot_slo') {
        return `${formatNumber(Number(value) * 1000)} ms`;
    }
    if (key === 'error_rate_slo') return `${formatNumber(Number(value))}%`;
    return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

export function responseStatusGuidance(statusCode) {
    if (statusCode === null || statusCode === undefined) return 'The request failed before an HTTP response code was available.';
    if (statusCode === 401 || statusCode === 403) return 'Check authentication, authorization, or tenant policy.';
    if (statusCode === 429) return 'The endpoint rate-limited requests; reduce or control offered load.';
    if (statusCode === 500) return 'The serving application encountered an internal error.';
    if (statusCode === 502 || statusCode === 503) return 'Check the upstream provider, gateway, availability, or failover path.';
    if (statusCode >= 400 && statusCode < 500) return 'The endpoint rejected the request; inspect request and policy details.';
    if (statusCode >= 500) return 'The server or an upstream dependency failed to serve the request.';
    return 'Inspect the response and task log for this status code.';
}
