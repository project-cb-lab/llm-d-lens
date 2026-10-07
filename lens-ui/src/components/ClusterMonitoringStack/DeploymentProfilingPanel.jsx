// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
    Activity, AlertTriangle, ArrowLeft, GitBranch, Maximize2,
    RotateCcw, ZoomIn, ZoomOut,
} from 'lucide-react';
import * as echarts from 'echarts/core';
import { SankeyChart } from 'echarts/charts';
import { TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { SideDrawer } from '../ui/SideDrawer.jsx';
import { Badge, Button, EmptyState, LoadingState, SectionLabel } from '../ui';
import { getDeploymentFlowMap } from './clusterMonitoringStackBackend';

echarts.use([SankeyChart, TooltipComponent, CanvasRenderer]);

// Sankey hands every node the same nodeWidth, but the users and output columns
// hold one word rather than a block of metric rows and look bloated at the full
// card width. Layout stages run in registration order and the built-in sankey
// layout is registered when the chart module is installed above, so this runs
// after it. Shrinking those two columns also frees horizontal space, so the
// remaining columns are re-spread to keep every gap equal — otherwise sankey's
// own spacing (computed from the uniform nodeWidth) would leave the two inner
// gaps too tight for their edge labels. Edge geometry is read from the node
// layout at render time, so the ribbons follow automatically.
echarts.registerLayout((ecModel) => {
    ecModel.eachSeriesByType('sankey', (seriesModel) => {
        if (!seriesModel.get('narrowEndColumns')) return;
        const graph = seriesModel.getGraph();

        const columns = new Map();
        let left = Infinity;
        let right = -Infinity;
        graph.eachNode((node) => {
            const layout = node.getLayout();
            if (!layout || layout.dx == null) return;
            const key = Math.round(layout.x);
            if (!columns.has(key)) columns.set(key, []);
            columns.get(key).push(node);
            left = Math.min(left, layout.x);
            right = Math.max(right, layout.x + layout.dx);
        });
        const keys = [...columns.keys()].sort((a, b) => a - b);
        if (keys.length < 2) return;

        // Only an end column is narrowed, and only when every node in it is a
        // pure source or pure sink — an interior role that happens to be first
        // or last still needs room for its metric rows.
        const widthOf = (key, index) => {
            const isEnd = index === 0 || index === keys.length - 1;
            const bare = columns.get(key).every((n) => n.inEdges.length === 0 || n.outEdges.length === 0);
            return isEnd && bare ? END_CARD_W : NODE_CARD_W;
        };
        const widths = keys.map(widthOf);
        const total = widths.reduce((sum, w) => sum + w, 0);
        const gap = (right - left - total) / (keys.length - 1);
        if (!(gap > 0)) return;

        let x = left;
        keys.forEach((key, index) => {
            columns.get(key).forEach((node) => node.setLayout({ x, dx: widths[index] }, true));
            x += widths[index] + gap;
        });
    });
});

const PROFILING_TABS = [
    { value: 'flow-map', label: 'Flow Map', icon: GitBranch },
];

// Real-time refresh floor: re-schedule the next fetch this many milliseconds
// after each response returns (self-rescheduling, so a slow backend never
// piles up overlapping requests).
const FLOW_REFRESH_MS = 800;

function formatRate(value, suffix) {
    if (value == null || !Number.isFinite(value)) return '—';
    if (value >= 1000) return `${(value / 1000).toFixed(1)}K ${suffix}`;
    if (value >= 100) return `${value.toFixed(0)} ${suffix}`;
    if (value >= 1) return `${value.toFixed(1)} ${suffix}`;
    return `${value.toFixed(2)} ${suffix}`;
}

function formatQueue(value) {
    if (value == null || !Number.isFinite(value)) return '—';
    if (value >= 1000) return `${(value / 1000).toFixed(1)}K`;
    return value.toFixed(0);
}

// Adaptive precision: KV-cache usage on a small model under light load sits
// well below 1% (e.g. 0.36%), and a fixed 0-decimal format would render every
// such reading as a flat "0%" — indistinguishable from genuinely idle.
function formatPercent(value) {
    if (value == null || !Number.isFinite(value)) return '—';
    if (value === 0) return '0%';
    const abs = Math.abs(value);
    if (abs >= 10) return `${value.toFixed(0)}%`;
    if (abs >= 1) return `${value.toFixed(1)}%`;
    if (abs >= 0.01) return `${value.toFixed(2)}%`;
    return value > 0 ? '<0.01%' : '>-0.01%';
}

const ROLE_COLOR = {
    users: '#94a3b8',
    epp: '#22d3ee',
    prefill: '#a78bfa',
    decode: '#34d399',
    output: '#f472b6',
};

const ROLE_SHORT = {
    epp: 'EPP', prefill: 'Prefill', decode: 'Decode', output: 'Output',
};

const LINK_COLOR = {
    success: '#b0c46b',
    error: '#e07a7a',
};

// Solid dark backdrop for the flow-map canvas. Each ribbon is outlined with
// this same colour so adjacent links show a small, clean gap between them
// (echarts sankey has no native link-gap option, so a background-coloured
// stroke is the reliable way to separate neighbouring ribbons).
const FLOW_GAP_COLOR = '#0b1120';

function FlowToolButton({ children, active, label, onClick }) {
    return (
        <button
            type="button"
            aria-label={label}
            title={label}
            onClick={onClick}
            className={`flex h-8 w-8 items-center justify-center rounded-full border transition-colors ${
                active
                    ? 'border-lime-300/40 bg-lime-400 text-slate-950'
                    : 'border-slate-700/60 bg-slate-900/60 text-slate-400 hover:text-slate-200'
            }`}
        >
            {children}
        </button>
    );
}

// Pod names end in a replica-set hash + pod hash, and llm-d appends a
// "-rank-N" worker suffix. Naively keeping the last two segments yields
// "rank-0" for every model-server pod, making all instances indistinguishable;
// drop the rank suffix first so the identifying hashes survive.
function shortInstanceName(name) {
    if (!name) return '';
    const trimmed = String(name).replace(/-rank-\d+$/, '');
    const parts = trimmed.split('-');
    return parts.length > 2 ? parts.slice(-2).join('-') : trimmed;
}

const FLOW_MAP_WIDTH = 1400;
const FLOW_MAP_HEIGHT = 680;
// Metrics live inside the node card, so the card is a card rather than the
// thin bar a sankey normally draws. echarts hard-codes sankey edge labels to
// the ribbon's centre (SankeyView sets textConfig.position = 'inside'), which
// is exactly where a label block parked beside the node would land — the two
// cannot share a gap at any realistic canvas width. Putting the metrics inside
// the card sidesteps the collision and makes the opaque card back the text, so
// the rows stay legible over a full-width ribbon.
const NODE_CARD_W = 208;
// The users and output columns carry a single word instead of metric rows, so
// they get a slimmer card. Sankey applies nodeWidth uniformly, so this is
// applied by narrowing their layout after the fact (see narrowEndColumns).
const END_CARD_W = 76;
const CHART_PAD_X = 30;
// Below this the inter-column gaps get too narrow for the edge rate labels, so
// the canvas stops shrinking with the panel and scrolls horizontally instead.
// Sized from the post-shrink card widths so the four gaps still clear the
// widest edge label ("fail 0.12 req/s") at the narrowest allowed canvas.
const FLOW_MAP_MIN_WIDTH = 2 * CHART_PAD_X + 3 * NODE_CARD_W + 2 * END_CARD_W + 4 * 105;
const NODE_GAP = 24;
const CHART_PAD_Y = 28;
// Vertical room a node needs for its label block (role 16px, instance 14px and
// up to four 14px metric rows = 86px) plus padding inside the card.
const MIN_NODE_PX = 104;

const MONO_FONT = 'ui-monospace, SFMono-Regular, Menlo, monospace';
// Metric rows render as three fixed-width columns — name, micro bar, value —
// so bars start on a common vertical line and values right-align. Without the
// fixed columns a per-instance stack of ragged rows is harder to scan than the
// plain text it replaces.
const LABEL_COL_W = 104;
const BAR_W = 40;
const BAR_H = 6;
const VALUE_COL_W = 30;
const VALUE_PAD_L = 4;
const BAR_TRACK = '#334155';
// Capacity-style metrics (how full is it) escalate cyan -> amber -> red.
const CAPACITY_STOPS = [[85, '#f87171'], [60, '#fbbf24'], [0, '#22d3ee']];
// Hit-rate metrics use one flat colour on purpose: a full bar means "good"
// here and "about to saturate" for capacity, so reusing the escalating palette
// would read 100% cache hit as an alarm.
const HIT_COLOR = '#a3e635';
// Queue depth is unbounded, so a linear bar either pins to full or squashes
// every normal reading to nothing. Log-scale against a fixed ceiling: the
// reference never drifts between frames (unlike scaling to the current max)
// and it matches how depth actually reads — 1 vs 10 matters far more than
// 100 vs 110.
const QUEUE_LOG_MAX = 128;

function capacityColor(percent) {
    for (const [floor, color] of CAPACITY_STOPS) {
        if (percent >= floor) return color;
    }
    return CAPACITY_STOPS[CAPACITY_STOPS.length - 1][1];
}

function queueFraction(value) {
    return Math.min(1, Math.log1p(Math.max(0, value)) / Math.log1p(QUEUE_LOG_MAX));
}

// Builds one row per metric: a full-word label, a micro bar, and the value.
// A null reading keeps its row but draws no bar at all — "no data" and "zero"
// must not look alike, which is exactly the confusion an empty track creates.
function metricRowsFor(node, downstreamRoles) {
    if (node.isUser || node.role === 'output') return [];
    const m = node.metrics || {};
    const rows = [];
    const addRow = (label, text, fraction, color) => {
        const hasBar = fraction != null;
        const fillW = hasBar ? Math.round(Math.min(1, Math.max(0, fraction)) * BAR_W) : 0;
        rows.push({ label, text, color, hasBar, fillW, restW: hasBar ? BAR_W - fillW : 0 });
    };

    const addDepthRow = (label, value) => {
        if (value != null && Number.isFinite(value)) {
            const fraction = queueFraction(value);
            addRow(label, formatQueue(value), fraction, capacityColor(fraction * 100));
        } else {
            addRow(label, '—', null, BAR_TRACK);
        }
    };

    // The EPP dispatches immediately and holds no admission buffer, so its
    // request_running gauge is in-flight work, not a backlog.
    addDepthRow(node.role === 'epp' ? 'in flight' : 'queue', m.queue_length);
    if (node.role === 'epp') {
        // Which phase that in-flight work is in. Requests spend almost all of
        // their life in decode, so a large "in flight" with a near-empty
        // prefill share is the healthy shape; the reverse means prefill is the
        // bottleneck. A role that is not in the pipeline gets no row at all —
        // an em dash there would imply a missing reading rather than a stage
        // that does not exist.
        if (downstreamRoles?.has('prefill')) addDepthRow('at prefill', m.prefill_inflight);
        if (downstreamRoles?.has('decode')) addDepthRow('at decode', m.decode_inflight);
        return rows;
    }

    const addPercentRow = (label, value, color) => {
        const ok = value != null && Number.isFinite(value);
        addRow(label, formatPercent(value), ok ? value / 100 : null, color(value));
    };
    addPercentRow('KV cache', m.kv_cache_usage_perc, capacityColor);
    addPercentRow('prefix cache hit', m.prefix_cache_hit_rate, () => HIT_COLOR);
    addPercentRow('external cache hit', m.external_prefix_cache_hit_rate, () => HIT_COLOR);
    return rows;
}

// Rich styles are attached per node (not per series), so each bar segment can
// carry its own exact pixel width instead of being quantised into shared
// buckets.
function nodeLabelRich(rows) {
    const rich = {
        role: { color: '#e2e8f0', fontSize: 11, fontWeight: 700, lineHeight: 16 },
        name: { color: '#f8fafc', fontSize: 9, fontFamily: MONO_FONT, lineHeight: 14 },
        lbl: { color: '#94a3b8', fontSize: 9, fontFamily: MONO_FONT, lineHeight: 14, width: LABEL_COL_W, align: 'left' },
        val: { color: '#f8fafc', fontSize: 9, fontFamily: MONO_FONT, lineHeight: 14, width: VALUE_COL_W, align: 'right', padding: [0, 0, 0, VALUE_PAD_L] },
        gap: { width: BAR_W, height: BAR_H, lineHeight: 14 },
    };
    (rows || []).forEach((row, i) => {
        if (!row.hasBar) return;
        // Round only the outer ends so the fill and the remaining track read as
        // a single pill rather than two abutting capsules.
        if (row.fillW > 0) {
            rich[`f${i}`] = {
                width: row.fillW, height: BAR_H, lineHeight: 14,
                backgroundColor: row.color,
                borderRadius: row.restW > 0 ? [3, 0, 0, 3] : 3,
            };
        }
        if (row.restW > 0) {
            rich[`r${i}`] = {
                width: row.restW, height: BAR_H, lineHeight: 14,
                backgroundColor: BAR_TRACK,
                borderRadius: row.fillW > 0 ? [0, 3, 3, 0] : 3,
            };
        }
    });
    return rich;
}

function nodeLabelFormatter(params) {
    const node = params.data || {};
    const roleLabel = node.isUser ? 'USERS' : ((ROLE_SHORT[node.role] || node.role || '').toUpperCase());
    const lines = [`{role|${roleLabel}}`];
    if (node.shortLabel) lines.push(`{name|${node.shortLabel}}`);
    (node.metricRows || []).forEach((row, i) => {
        let bar = '{gap|}';
        if (row.hasBar) {
            bar = `${row.fillW > 0 ? `{f${i}|}` : ''}${row.restW > 0 ? `{r${i}|}` : ''}`;
        }
        lines.push(`{lbl|${row.label}}${bar}{val|${row.text}}`);
    });
    return lines.join('\n');
}

// Builds an ECharts sankey option from live flow-map data. Nodes are pure-text
// rounded rectangles with their role / instance / queue rendered beside the
// card (like a normal Sankey). Links are curved ribbons whose thickness is
// proportional to throughput: every adjacent stage pair carries two ribbons —
// an "ok" ribbon for requests that advance and a "fail" ribbon for the ones
// that fail at that hop — so failures stay visible at the stage where they
// occurred instead of pooling into a single Failed sink.
function buildSankeyOption(present, metric) {
    const showErrors = metric === 'request';
    // Which serving roles are actually in this pipeline, so the EPP card only
    // claims a per-role queue for stages that exist.
    const downstreamRoles = new Set(present.map((component) => component.role));

    // One column per stage: users, each present role's instances, then the
    // output sink column.
    const columns = [[{
        id: 'users',
        role: 'users',
        label: 'Users',
        isUser: true,
        metrics: null,
        shortLabel: '',
    }]];
    for (const component of present) {
        const instances = component.instances || [];
        const nodeFor = (instance, index) => {
            const name = typeof instance === 'string' ? instance : (instance && instance.name);
            return {
                id: `${component.role}:${name || index}`,
                role: component.role,
                label: name || component.label,
                shortLabel: shortInstanceName(name),
                isUser: false,
                metrics: (typeof instance === 'object' && instance) ? instance : component,
            };
        };
        // Fall back to a single aggregate node if no per-instance breakdown exists.
        columns.push(instances.length
            ? instances.map(nodeFor)
            : [nodeFor({ name: component.label, ...component }, 0)]);
    }
    const sinkCol = [{
        id: 'output',
        role: 'output',
        label: 'Output',
        isUser: false,
        metrics: null,
        shortLabel: '',
    }];
    columns.push(sinkCol);

    // Per-stage flow: how much succeeds (continues downstream) and how much
    // fails (becomes the hop's fail ribbon). Token flow has no success/fail
    // split.
    // `measured` distinguishes "this stage reported 0 req/s" from "this stage
    // reported nothing at all" (its exporter is unreachable, e.g. the EPP's
    // /metrics is behind an authenticated endpoint Prometheus cannot scrape).
    // Both used to collapse to 0 and silently blank out every hop touching the
    // stage, making a busy pipeline look idle.
    const flowOf = (node) => {
        if (node.isUser || node.role === 'output') {
            return { success: 0, error: 0, measured: true };
        }
        const m = node.metrics || {};
        if (metric === 'token') {
            const inputRate = m.input_token_rate;
            const outputRate = m.output_token_rate;
            const measured = Number.isFinite(inputRate) || Number.isFinite(outputRate);
            const value = (inputRate ?? 0) + (outputRate ?? 0);
            return { success: measured && value > 0 ? value : 0, error: 0, measured };
        }
        let success = m.success_request_rate ?? null;
        let fail = m.failed_request_rate ?? null;
        if (success == null && fail == null && m.request_rate != null) {
            success = m.request_rate;
            fail = 0;
        }
        return {
            success: Number.isFinite(success) && success > 0 ? success : 0,
            error: Number.isFinite(fail) && fail > 0 ? fail : 0,
            measured: Number.isFinite(success) || Number.isFinite(fail),
        };
    };

    const nodes = columns.flat();
    for (const node of nodes) node.flow = flowOf(node);

    // Endpoint flow: users emits the total ingress into the first stage, and
    // output consumes the last stage's successes and failures. With no shared
    // Failed sink, each node's inflow == outflow (success + fail both keep
    // flowing stage-to-stage as parallel ok/fail ribbons).
    const stageCols = columns.slice(1, -1);
    if (stageCols.length) {
        // Ingress is read from the first stage that actually reports metrics.
        // When the entry stage has no exporter, deriving ingress from it would
        // report zero traffic for the whole pipeline.
        const measuredIdx = stageCols.findIndex((col) => col.some((n) => n.flow.measured));
        const ingressIdx = measuredIdx >= 0 ? measuredIdx : 0;
        const first = stageCols[ingressIdx];
        const last = stageCols[stageCols.length - 1];
        // The users -> first-stage hop is sourced from the first stage's OWN
        // Prometheus metrics (not a bound simulation task), matching the
        // downstream hops' caliber. It uses the stage's ARRIVAL rate rather
        // than its completion rate: vLLM only exposes a completion counter
        // (request_success_total), so the backend reconstructs arrival as
        // completion + the waiting/running depth delta. That lets the entry
        // hop exceed the exit hop while the queue absorbs a burst, instead of
        // always matching it. The ok ribbon carries arrival minus the abort /
        // error portion; the timeout / backend ribbons carry abort / error, so
        // users' total outflow equals the arrival rate and stays conserved.
        const ingress = first.reduce((sum, n) => sum + n.flow.success + n.flow.error, 0);
        const firstComponent = present[ingressIdx] || {};
        const timeout = showErrors && Number.isFinite(firstComponent.client_timeout_rate) && firstComponent.client_timeout_rate > 0
            ? firstComponent.client_timeout_rate
            : 0;
        const backend = showErrors && Number.isFinite(firstComponent.backend_error_rate) && firstComponent.backend_error_rate > 0
            ? firstComponent.backend_error_rate
            : 0;
        const arrival = Number.isFinite(firstComponent.arrival_request_rate)
            ? firstComponent.arrival_request_rate
            : null;
        let ingressSuccess;
        if (!showErrors) {
            // Token mode: the ingress hop must carry the same token rate as the
            // rest of the chain. The request-rate reconstruction below is
            // req/s and would render a token hop in the wrong unit entirely.
            ingressSuccess = ingress;
        } else if (Number.isFinite(arrival)) {
            // Arrival already includes the abort/error portion (they finished,
            // so they were counted as arrivals), so subtract them before
            // splitting the remainder into ok + timeout + backend ribbons.
            ingressSuccess = Math.max(0, arrival - timeout - backend);
        } else if (Number.isFinite(firstComponent.request_rate)) {
            ingressSuccess = Math.max(0, firstComponent.request_rate - timeout - backend);
        } else if (Number.isFinite(firstComponent.success_request_rate)) {
            ingressSuccess = Math.max(0, firstComponent.success_request_rate);
        } else {
            ingressSuccess = ingress;
        }
        columns[0][0].flow = {
            success: ingressSuccess,
            error: showErrors ? timeout + backend : 0,
            measured: measuredIdx >= 0,
        };
        columns[0][0].clientTimeout = showErrors ? timeout : 0;
        columns[0][0].clientBackend = showErrors ? backend : 0;
        const outputNode = sinkCol.find((n) => n.role === 'output');
        outputNode.flow = {
            success: last.reduce((sum, n) => sum + n.flow.success, 0),
            error: last.reduce((sum, n) => sum + n.flow.error, 0),
        };
    }
    for (const node of nodes) node.total = node.flow.success + node.flow.error;

    const indexOfId = new Map(nodes.map((n, i) => [n.id, i]));

    // Per-column flow totals, and a lookup for the nearest column that saw any
    // traffic. Requests are conserved end to end, so a run of stages reporting
    // nothing still has to carry whatever the rest of the chain observed.
    const colFlow = columns.map((col) => ({
        success: col.reduce((sum, n) => sum + (n.flow.success || 0), 0),
        error: col.reduce((sum, n) => sum + (n.flow.error || 0), 0),
    }));
    const nearestFlow = (ci, key) => {
        for (let d = 0; d < columns.length; d += 1) {
            if (colFlow[ci + d] && colFlow[ci + d][key] > 0) return colFlow[ci + d][key];
            if (colFlow[ci - d] && colFlow[ci - d][key] > 0) return colFlow[ci - d][key];
        }
        return 0;
    };

    // Success/fail chain: consecutive columns, distributing each source's
    // success and failure across the downstream column proportional to the
    // target's total. Every adjacent hop carries an ok ribbon, plus a fail
    // ribbon only where something actually failed (and distinct timeout / 4xx
    // ribbons on the users -> first-stage hop).
    const rawLinks = [];
    for (let ci = 0; ci < columns.length - 1; ci += 1) {
        const colA = columns[ci];
        const colB = columns[ci + 1];
        const totalB = colB.reduce((sum, n) => sum + (n.total || 0), 0);
        // A stage that observed no traffic borrows from the side that did,
        // rather than zeroing the hop. This matters beyond missing metrics: in
        // P/D a cache-served request never reaches prefill, so prefill can
        // legitimately report 0 completions while EPP is admitting requests.
        // Driving each hop off whichever neighbour saw traffic keeps the chain
        // connected instead of making the flow vanish mid-diagram.
        for (const a of colA) {
            for (const t of colB) {
                // Weights apportion a source's outflow across the target
                // column; with no target-side totals to weigh by, spread evenly.
                const share = totalB > 0 ? ((t.total || 0) / totalB) : (1 / colB.length);
                const relaySuccess = () => {
                    if (colFlow[ci].success > 0) return (a.flow.success || 0) * share;
                    if (colFlow[ci + 1].success > 0) return (t.flow.success || 0) / colA.length;
                    return nearestFlow(ci, 'success') / (colA.length * colB.length);
                };
                rawLinks.push({ source: a.id, target: t.id, status: 'success', realValue: relaySuccess() });
                if (!showErrors) continue;
                if (a.isUser) {
                    // Client-side failures (timeout / 4xx) ride the users ->
                    // first-stage hop and never reach vLLM, so they don't show
                    // up in any stage's Prometheus fail rate. Emit them as
                    // distinct fail ribbons alongside the ok ribbon; when
                    // nothing failed the hop carries the ok ribbon alone.
                    const timeout = a.clientTimeout || 0;
                    const backend = a.clientBackend || 0;
                    if (timeout > 0) rawLinks.push({ source: a.id, target: t.id, status: 'error', failKind: 'timeout', realValue: timeout * share });
                    if (backend > 0) rawLinks.push({ source: a.id, target: t.id, status: 'error', failKind: 'backend', realValue: backend * share });
                } else {
                    // Stage-level failures (abort/error at this stage) become
                    // the hop's fail ribbon, flowing toward the next stage.
                    // Unlike successes these only travel downstream: a request
                    // that failed at a later stage crossed this hop just fine,
                    // so a fail rate must never be back-propagated upstream.
                    // An entirely unmeasured source still borrows the target,
                    // otherwise its fail ribbon would be lost outright.
                    const sourceMeasured = colA.some((n) => n.flow.measured);
                    let stageFail = 0;
                    if (colFlow[ci].error > 0) stageFail = (a.flow.error || 0) * share;
                    else if (!sourceMeasured) stageFail = (t.flow.error || 0) / colA.length;
                    rawLinks.push({ source: a.id, target: t.id, status: 'error', failKind: 'stage', realValue: stageFail });
                }
            }
        }
    }

    const maxReal = rawLinks.reduce((max, l) => Math.max(max, l.realValue), 0);

    // A hop with no failures must not advertise one: the ribbon is already
    // zero-width and invisible, so all it can contribute is a stray red
    // "fail 0.00 req/s" label. Drop it entirely — the ok ribbon still carries
    // the hop, so the topology stays complete either way.
    const keptLinks = rawLinks.filter((l) => l.status !== 'error' || l.realValue > 0);

    // ECharts sizes a sankey node from the flow through it, so a stage with no
    // traffic collapses to a hairline and its right-hand label (role, instance
    // name, queue and cache lines) lands on top of its neighbours'. A node's
    // laid-out height is max(inflow, outflow, node.value) while a ribbon's
    // width comes from its own value alone, so a floor on the node value buys
    // box height without ever padding a ribbon: a 0 req/s hop still draws as a
    // zero-width line, and only the box it connects to stays legible.
    const inflow = new Map();
    const outflow = new Map();
    for (const l of keptLinks) {
        outflow.set(l.source, (outflow.get(l.source) || 0) + l.realValue);
        inflow.set(l.target, (inflow.get(l.target) || 0) + l.realValue);
    }
    const columnValues = columns.map((col) => col.map(
        (n) => Math.max(inflow.get(n.id) || 0, outflow.get(n.id) || 0),
    ));

    // The floor can only hand a node as much room as the canvas has to give:
    // once a column holds enough instances that an even split drops below the
    // label block, every node in it overlaps no matter what the traffic is.
    // Grow the canvas instead — the panel viewport scrolls.
    const widestColumn = columns.reduce((max, col) => Math.max(max, col.length), 1);
    const chartHeight = Math.max(
        FLOW_MAP_HEIGHT,
        CHART_PAD_Y * 2 + widestColumn * MIN_NODE_PX + (widestColumn - 1) * NODE_GAP,
    );

    // ECharts scales the whole diagram by ky = min over columns of (usable
    // height / column value sum), so raising the floor also shrinks ky. The
    // resulting node height is monotonic in the floor but has no closed form,
    // so bisect for the smallest floor that still buys MIN_NODE_PX — keeping
    // the live ribbons as close to their natural scale as possible.
    const innerHeight = chartHeight - CHART_PAD_Y * 2;
    const flooredNodePx = (floor) => {
        let ky = Infinity;
        columnValues.forEach((col) => {
            const sum = col.reduce((acc, v) => acc + Math.max(v, floor), 0);
            if (sum > 0) ky = Math.min(ky, (innerHeight - (col.length - 1) * NODE_GAP) / sum);
        });
        return Number.isFinite(ky) ? floor * ky : 0;
    };
    let lo = 0;
    let hi = Math.max(maxReal, 1);
    for (let i = 0; i < 40; i += 1) {
        const mid = (lo + hi) / 2;
        if (flooredNodePx(mid) >= MIN_NODE_PX) hi = mid; else lo = mid;
    }
    const nodeFloor = hi;

    const links = keptLinks.map((l) => ({
        source: indexOfId.get(l.source),
        target: indexOfId.get(l.target),
        status: l.status,
        failKind: l.failKind,
        realValue: l.realValue,
        value: l.realValue,
    }));

    const unit = metric === 'token' ? 'tok/s' : 'req/s';

    const echartsNodes = columns.flatMap((col, depth) => col.map((node) => {
        const accent = ROLE_COLOR[node.role] || '#94a3b8';
        const metricRows = metricRowsFor(node, downstreamRoles);
        return {
            name: node.id,
            depth,
            value: nodeFloor,
            role: node.role,
            fullName: node.label,
            shortLabel: node.shortLabel,
            isUser: node.isUser,
            metrics: node.metrics,
            metricRows,
            itemStyle: { color: '#16161f', borderColor: accent, borderWidth: 1.5, borderRadius: 10 },
            label: {
                position: 'inside',
                rich: nodeLabelRich(metricRows),
            },
        };
    }));

    const echartsLinks = links.map((l) => ({
        name: `${nodes[l.source].id}->${nodes[l.target].id}:${l.status}`,
        source: nodes[l.source].id,
        target: nodes[l.target].id,
        value: l.value,
        status: l.status,
        failKind: l.failKind,
        realValue: l.realValue,
        lineStyle: {
            color: LINK_COLOR[l.status] || LINK_COLOR.success,
            opacity: 0.8,
            borderColor: FLOW_GAP_COLOR,
            borderWidth: 2,
        },
    }));

    const byId = new Map(echartsNodes.map((n) => [n.name, n]));

    const edgeLabel = (params) => {
        const link = params.data || {};
        // A zero-flow hop is drawn at zero width, so it has no visible ribbon.
        // Labelling it anyway leaves a stray "fail 0.00 req/s" floating in
        // empty space.
        if (!(link.value > 0)) return '';
        if (metric === 'token') return formatRate(link.realValue, unit);
        if (link.status === 'success') return `ok ${formatRate(link.realValue, unit)}`;
        const kind = link.failKind === 'timeout' ? 'timeout' : link.failKind === 'backend' ? '4xx' : 'fail';
        return `${kind} ${formatRate(link.realValue, unit)}`;
    };

    const endpointName = (node) => {
        if (node.isUser) return 'Users';
        if (node.role === 'output') {
            return ROLE_SHORT[node.role] || node.role;
        }
        return node.fullName || node.name;
    };

    const tooltip = (params) => {
        if (params.dataType === 'edge') {
            const link = params.data || {};
            const src = byId.get(link.source) || {};
            const tgt = byId.get(link.target) || {};
            const rows = [`<b>${endpointName(src)}</b> → <b>${endpointName(tgt)}</b>`, edgeLabel({ data: link })].filter(Boolean);
            if (link.status === 'error' && link.failKind === 'timeout') {
                rows.push('client timeout — no HTTP response');
            } else if (link.status === 'error' && link.failKind === 'backend') {
                rows.push('backend HTTP error (e.g. 400)');
            } else if (link.status === 'error' && link.failKind === 'stage') {
                rows.push('requests failed at this stage');
            }
            return rows.join('<br/>');
        }
        const node = params.data || {};
        const roleLabel = node.isUser ? 'Users' : (ROLE_SHORT[node.role] || node.role || '');
        const m = node.metrics || {};
        if (node.isUser || node.role === 'output') {
            return `<b>${roleLabel}</b>${node.fullName ? ` · ${node.fullName}` : ''}`;
        }
        const rows = [`<b>${roleLabel}</b>${node.fullName ? ` · ${node.fullName}` : ''}`];
        if (metric === 'token') {
            rows.push(`tokens: ${formatRate(m.input_token_rate, 'tok/s')} in · ${formatRate(m.output_token_rate, 'tok/s')} out`);
        } else {
            if (m.arrival_request_rate != null && Number.isFinite(m.arrival_request_rate)) {
                rows.push(`arrival: ${formatRate(m.arrival_request_rate, 'req/s')}`);
            }
            rows.push(`request: ${formatRate(m.request_rate, 'req/s')}`);
            rows.push(`ok: ${formatRate(m.success_request_rate, 'req/s')} · fail: ${formatRate(m.failed_request_rate, 'req/s')}`);
        }
        if (m.queue_length != null && Number.isFinite(m.queue_length)) {
            rows.push(`queue: ${formatQueue(m.queue_length)}`);
        }
        if (m.kv_cache_usage_perc != null && Number.isFinite(m.kv_cache_usage_perc)) {
            rows.push(`KV cache usage: ${formatPercent(m.kv_cache_usage_perc)}`);
        }
        if (m.prefix_cache_hit_rate != null && Number.isFinite(m.prefix_cache_hit_rate)) {
            rows.push(`prefix cache hit: ${formatPercent(m.prefix_cache_hit_rate)}`);
        }
        if (m.external_prefix_cache_hit_rate != null && Number.isFinite(m.external_prefix_cache_hit_rate)) {
            rows.push(`external prefix cache hit: ${formatPercent(m.external_prefix_cache_hit_rate)}`);
        }
        return rows.join('<br/>');
    };

    return {
        height: chartHeight,
        option: {
            animationDuration: 200,
            animationDurationUpdate: 300,
            tooltip: {
                trigger: 'item',
                backgroundColor: 'rgba(15, 23, 42, 0.95)',
                borderColor: '#334155',
                borderWidth: 1,
                padding: [8, 12],
                textStyle: { color: '#e2e8f0', fontSize: 12 },
                formatter: tooltip,
            },
            series: [{
                type: 'sankey',
                left: CHART_PAD_X,
                top: CHART_PAD_Y,
                right: CHART_PAD_X,
                bottom: CHART_PAD_Y,
                nodeWidth: NODE_CARD_W,
                narrowEndColumns: true,
                nodeGap: NODE_GAP,
                nodeAlign: 'justify',
                layoutIterations: 32,
                draggable: true,
                emphasis: { focus: 'adjacency' },
                data: echartsNodes,
                links: echartsLinks,
                label: {
                    show: true,
                    position: 'right',
                    distance: 8,
                    formatter: nodeLabelFormatter,
                },
                edgeLabel: {
                    show: true,
                    position: 'inside',
                    fontSize: 10,
                    fontWeight: 700,
                    color: '#ffffff',
                    textBorderColor: '#0f0f14',
                    textBorderWidth: 2,
                    formatter: edgeLabel,
                },
                itemStyle: {
                    color: '#16161f',
                    borderWidth: 1.5,
                    borderColor: '#475569',
                    borderRadius: 10,
                },
                lineStyle: {
                    color: 'source',
                    curveness: 0.5,
                },
            }],
        },
    };
}

export function FlowMap({ data, metric, onRefresh, historical = false }) {
    const containerRef = useRef(null);
    const chartRef = useRef(null);
    const scrollRef = useRef(null);
    const [containerWidth, setContainerWidth] = useState(FLOW_MAP_WIDTH);
    const [zoom, setZoom] = useState(1);
    const [selectedName, setSelectedName] = useState(null);

    const zoomIn = () => setZoom((current) => Math.min(current + 0.25, 3));
    const zoomOut = () => setZoom((current) => Math.max(current - 0.25, 0.5));
    const fitView = () => setZoom(1);
    const resetView = () => {
        setZoom(1);
        if (onRefresh) onRefresh();
    };

    const { option, height: chartHeight } = useMemo(() => {
        const present = (data.components || []).filter((component) => component.present);
        return buildSankeyOption(present, metric);
    }, [data, metric]);

    useEffect(() => {
        if (!containerRef.current) return undefined;
        const chart = echarts.init(containerRef.current);
        chartRef.current = chart;
        chart.on('click', params => { if (params.dataType === 'node' && params.data?.metrics) setSelectedName(params.data.name); });
        const onResize = () => chart.resize();
        window.addEventListener('resize', onResize);
        return () => {
            window.removeEventListener('resize', onResize);
            chart.dispose();
            chartRef.current = null;
        };
    }, []);

    useEffect(() => {
        const el = scrollRef.current;
        if (!el) return undefined;
        const update = () => setContainerWidth(el.clientWidth || FLOW_MAP_WIDTH);
        update();
        const observer = new ResizeObserver(update);
        observer.observe(el);
        return () => observer.disconnect();
    }, []);

    useEffect(() => {
        const chart = chartRef.current;
        if (!chart) return;
        chart.setOption(option);
    }, [option]);

    useEffect(() => {
        const chart = chartRef.current;
        if (!chart) return;
        chart.resize();
    }, [zoom, containerWidth, chartHeight]);

    // Never shrink past the width the five node cards plus their edge labels
    // need; the scroll container takes over below that.
    const width = Math.max(containerWidth, FLOW_MAP_MIN_WIDTH);
    const inspectable = (option.series?.[0]?.data || []).filter(node => node.metrics);
    const selectedNode = inspectable.find(node => node.name === selectedName);

    return (
        <div className="w-full">
            {inspectable.length > 0 && <div role="group" aria-label="Inspect live flow nodes" className="mb-3 flex flex-wrap gap-2">{inspectable.map(node => <button type="button" key={node.name} aria-label={`Inspect ${node.fullName}`} title={node.fullName} aria-pressed={selectedNode?.name === node.name} onClick={() => setSelectedName(node.name)} className="max-w-full break-all rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-left text-xs text-slate-300 hover:border-cyan-500 hover:text-cyan-200">{ROLE_SHORT[node.role] || node.role} · {node.shortLabel || node.fullName}</button>)}</div>}
            {selectedNode && <SideDrawer title={selectedNode.fullName} subtitle={`${historical ? 'Recorded' : 'Live'} ${selectedNode.role} metrics`} onClose={() => setSelectedName(null)}><p className="mb-4 text-xs leading-5 text-slate-400">{historical ? 'Benchmark window averages' : 'Current metrics'} · {data.namespace}</p><dl className="space-y-3">{[['request_rate', 'Request rate', 'req/s'], ['success_request_rate', 'Successful requests', 'req/s'], ['failed_request_rate', 'Failed requests', 'req/s'], ['input_token_rate', 'Input token rate', 'tok/s'], ['output_token_rate', 'Output token rate', 'tok/s'], ['queue_length', 'Queue / in-flight', 'requests'], ['prefill_inflight', 'At Prefill', 'requests'], ['decode_inflight', 'At Decode', 'requests'], ['kv_cache_usage_perc', 'KV cache usage', '%'], ['prefix_cache_hit_rate', 'Prefix cache hit ratio', '%'], ['external_prefix_cache_hit_rate', 'External cache hit ratio', '%']].filter(([key]) => Number.isFinite(selectedNode.metrics[key])).map(([key, label, unit]) => <div key={key} className="flex items-center justify-between gap-3 rounded-lg border border-slate-800 p-3 text-xs"><dt className="text-slate-400">{label}</dt><dd className="font-mono text-sky-200">{Number.isFinite(selectedNode.metrics[key]) ? `${selectedNode.metrics[key].toLocaleString(undefined, { maximumFractionDigits: 2 })} ${unit}` : 'Not collected'}</dd></div>)}</dl>{!Object.entries(selectedNode.metrics).some(([key, value]) => key !== 'name' && Number.isFinite(value)) && <p className="text-xs text-slate-400">No metrics received for this node yet.</p>}</SideDrawer>}
            <div className="mb-3 flex items-center justify-end gap-1">
                <FlowToolButton label="Zoom in" onClick={zoomIn}><ZoomIn size={15} /></FlowToolButton>
                <FlowToolButton label="Zoom out" onClick={zoomOut}><ZoomOut size={15} /></FlowToolButton>
                <FlowToolButton label="Reset view" active={zoom === 1} onClick={resetView}><RotateCcw size={15} /></FlowToolButton>
                <FlowToolButton label="Fit view" onClick={fitView}><Maximize2 size={15} /></FlowToolButton>
            </div>
            <div className="relative min-w-0 overflow-hidden rounded-xl border border-slate-800/60 bg-slate-950/50">
                <div ref={scrollRef} className="overflow-auto" style={{ maxHeight: FLOW_MAP_HEIGHT + 24 }}>
                    <div className="relative">
                        <div ref={containerRef} style={{ width: width * zoom, height: chartHeight * zoom, backgroundColor: FLOW_GAP_COLOR }} />
                    </div>
                </div>
            </div>
        </div>
    );
}
function MetricToggle({ value, onChange }) {
    const options = [
        { value: 'request', label: 'Request' },
        { value: 'token', label: 'Token' },
    ];
    return (
        <div className="inline-flex items-center gap-1 rounded-lg border border-slate-700/70 bg-slate-900/60 p-1" role="tablist" aria-label="Flow metric">
            {options.map((option) => {
                const active = option.value === value;
                return (
                    <button
                        key={option.value}
                        type="button"
                        role="tab"
                        aria-selected={active}
                        onClick={() => onChange(option.value)}
                        className={`rounded-md px-3 py-1 text-xs font-semibold transition-colors ${
                            active ? 'bg-cyan-500/20 text-cyan-200 ring-1 ring-cyan-400/40' : 'text-slate-400 hover:text-slate-200'
                        }`}
                    >
                        {option.label}
                    </button>
                );
            })}
        </div>
    );
}

export function DeploymentProfilingPanel({ clusterId, deployment, onBack, refreshMs = FLOW_REFRESH_MS }) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [metric, setMetric] = useState('request');
    const [activeTab, setActiveTab] = useState('flow-map');
    const [revision, setRevision] = useState(0);
    const executionId = deployment?.execution_id;

    useEffect(() => {
        if (!executionId) return undefined;
        const controller = new AbortController();
        let cancelled = false;
        let timer = null;
        const load = async (silent = false) => {
            if (!silent) setLoading(true);
            try {
                const payload = await getDeploymentFlowMap(executionId, { clusterId, signal: controller.signal });
                if (cancelled) return;
                setData(payload);
                setError(null);
            } catch (nextError) {
                if (!cancelled) setError(nextError.message);
            } finally {
                if (!cancelled) setLoading(false);
            }
            // Real-time refresh: re-schedule after each response instead of a
            // fixed interval so slow backends never overlap in-flight requests.
            if (!cancelled) {
                timer = window.setTimeout(() => load(true), refreshMs);
            }
        };
        load();
        return () => {
            cancelled = true;
            controller.abort();
            if (timer) window.clearTimeout(timer);
        };
    }, [clusterId, executionId, refreshMs, revision]);

    const refresh = () => setRevision(value => value + 1);

    const presentCount = (data?.components || []).filter((component) => component.present).length;

    return (
        <section className="relative overflow-hidden rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/80 via-slate-900/40 to-slate-950/90 p-5 shadow-xl backdrop-blur-xl">
            <div className="relative">
                <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                    <div className="flex items-center gap-3">
                        {onBack && <Button variant="ghost" size="sm" onClick={onBack} className="px-2" aria-label="Back to deployments">
                            <ArrowLeft size={16} />
                        </Button>}
                        <div>
                            <div className="flex items-center gap-2">
                                <SectionLabel tone="violet">Deployment profiling</SectionLabel>
                                {data?.prometheus_reachable === false && (
                                    <Badge tone="warning" size="xs">Prometheus unreachable</Badge>
                                )}
                            </div>
                            <p className="mt-1 font-mono text-xs text-slate-400">
                                {deployment?.name || deployment?.guide || 'Deployment'}
                                {deployment?.namespace ? ` · ${deployment.namespace}` : ''}
                            </p>
                        </div>
                    </div>
                    <MetricToggle value={metric} onChange={setMetric} />
                </div>

                <div
                    className="mb-4 inline-flex flex-wrap items-center gap-1 rounded-2xl border border-slate-800/80 bg-slate-900/50 p-1.5"
                    role="tablist"
                    aria-label="Profiling angle"
                >
                    {PROFILING_TABS.map((tab) => {
                        const active = tab.value === activeTab;
                        const Icon = tab.icon;
                        return (
                            <button
                                key={tab.value}
                                type="button"
                                role="tab"
                                aria-selected={active}
                                onClick={() => setActiveTab(tab.value)}
                                className={`inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-semibold transition-all ${
                                    active
                                        ? 'bg-gradient-to-r from-violet-500/20 to-cyan-500/15 text-white ring-1 ring-violet-400/30'
                                        : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-100'
                                }`}
                            >
                                <Icon className={`h-4 w-4 ${active ? 'text-violet-300' : 'text-slate-500'}`} />
                                {tab.label}
                            </button>
                        );
                    })}
                </div>

                {error && (
                    <div role="alert" className="mb-4 flex items-start gap-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
                        <AlertTriangle size={16} className="mt-0.5 shrink-0" />
                        <span>{error}</span>
                    </div>
                )}

                {data?.message && presentCount > 0 && (
                    <div role="status" className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
                        {data.message}
                    </div>
                )}

                {loading && !data ? (
                    <LoadingState label="Building flow map…" />
                ) : presentCount === 0 ? (
                    <EmptyState
                        icon={<Activity className="h-10 w-10" />}
                        title="No serving components found"
                        message={data?.message || 'This deployment has no EPP, prefill, or decode components to visualize.'}
                    />
                ) : (
                    <div className="rounded-xl border border-slate-800/60 bg-slate-950/50 p-4">
                        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                            <div className="flex items-center gap-2 text-xs text-slate-400">
                                <Activity size={14} className="text-cyan-300" />
                                <span>
                                    Ribbon width is proportional to {metric === 'token' ? 'token throughput' : 'request rate'}.
                                </span>
                                <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-amber-300">
                                    <span className="relative flex h-1.5 w-1.5">
                                        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-amber-400" />
                                    </span>
                                    Sampled every 5s
                                </span>
                            </div>
                            {metric === 'request' && (
                                <div className="flex items-center gap-4 text-[10px] uppercase tracking-wider text-slate-500">
                                    <span className="flex items-center gap-1.5"><span className="h-1 w-6 rounded bg-gradient-to-r from-lime-400 to-green-500" /> success</span>
                                    <span className="flex items-center gap-1.5"><span className="h-1 w-6 rounded bg-gradient-to-r from-red-400 to-red-700" /> failed</span>
                                </div>
                            )}
                        </div>
                        <FlowMap data={data} metric={metric} onRefresh={refresh} />
                    </div>
                )}
            </div>
        </section>
    );
}
