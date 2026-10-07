import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Activity } from 'lucide-react';
import {
    Area,
    AreaChart,
    Bar,
    BarChart,
    CartesianGrid,
    Cell,
    Pie,
    PieChart,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import { usePolling } from '../../hooks/usePolling';
import { AsyncState } from '../shared/AsyncState';
import { MultiSelectDropdown } from '../common';
import { Button } from '../ui/Button';
import { EmptyState } from '../ui/EmptyState';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { Input, Select } from '../ui/FormControls';
import { SectionLabel } from '../ui/SectionLabel';
import { StatCard } from '../ui/StatCard';
import { ToggleGroup } from '../ui/ToggleGroup';
import { ChartContainer } from '../ui/charts/ChartContainer';
import { ChartLegend } from '../ui/charts/ChartLegend';
import { ChartTooltip, ChartTooltipRow } from '../ui/charts/ChartTooltip';
import { cn } from '../../utils/cn';
import {
    getUsageAnalytics,
    listModelTokens,
    listUsageRecords,
} from './modelServiceBackend';
import { CARD, CELL, CELL_MUTED, TABLE_HEAD, TABLE_ROW, maskedKeyHint } from './modelServiceStyles';

const numberFormat = new Intl.NumberFormat();
// Match the 400-level palette used by the other Prism dashboards (reads well on
// the dark surface). Order: emerald, sky, amber, violet, rose.
const USAGE_COLORS = ['#34d399', '#38bdf8', '#fbbf24', '#a78bfa', '#fb7185'];
const OTHER_COLOR = '#94a3b8';
const AXIS = { fontSize: 10 };
const AXIS_PROPS = { tick: AXIS, stroke: '#64748b', axisLine: false, tickLine: false };

const RANGE_OPTIONS = [
    { value: 'today', label: 'Today' },
    { value: 'yesterday', label: 'Yesterday' },
    { value: '7d', label: 'Last 7 days' },
    { value: '30d', label: 'Last 30 days' },
    { value: 'this_month', label: 'This month' },
    { value: 'last_month', label: 'Last month' },
    { value: 'custom', label: 'Custom…' },
];

const startOfDay = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate(), 0, 0, 0, 0);
const endOfDay = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate(), 23, 59, 59, 999);

// Inclusive [since, until] Date bounds for a range choice (local time).
function rangeBounds(range, customFrom, customTo) {
    const now = new Date();
    switch (range) {
        case 'today':
            return { since: startOfDay(now), until: endOfDay(now) };
        case 'yesterday': {
            const y = new Date(now);
            y.setDate(now.getDate() - 1);
            return { since: startOfDay(y), until: endOfDay(y) };
        }
        case '7d': {
            const start = new Date(now);
            start.setDate(now.getDate() - 6);
            return { since: startOfDay(start), until: endOfDay(now) };
        }
        case '30d': {
            const start = new Date(now);
            start.setDate(now.getDate() - 29);
            return { since: startOfDay(start), until: endOfDay(now) };
        }
        case 'this_month':
            return { since: new Date(now.getFullYear(), now.getMonth(), 1), until: endOfDay(now) };
        case 'last_month':
            return {
                since: new Date(now.getFullYear(), now.getMonth() - 1, 1),
                until: new Date(now.getFullYear(), now.getMonth(), 0, 23, 59, 59, 999),
            };
        case 'custom':
            return {
                since: customFrom ? startOfDay(new Date(`${customFrom}T00:00:00`)) : null,
                until: customTo ? endOfDay(new Date(`${customTo}T00:00:00`)) : null,
            };
        default:
            return { since: null, until: null };
    }
}

function isHourly(bounds) {
    if (!bounds.since || !bounds.until) return false;
    return (bounds.until.getTime() - bounds.since.getTime()) <= 2 * 24 * 3600 * 1000;
}

const METRICS = [
    { value: 'tokens', label: 'Tokens' },
    { value: 'input', label: 'Input tokens' },
    { value: 'output', label: 'Output tokens' },
];
const METRIC_COLOR = { tokens: USAGE_COLORS[0], input: USAGE_COLORS[1], output: USAGE_COLORS[3] };

const COMPOSITION = [
    { key: 'input', name: 'Input', color: USAGE_COLORS[1] },
    { key: 'output', name: 'Output', color: USAGE_COLORS[0] },
];

function totalsOf(records) {
    return records.reduce(
        (acc, record) => ({
            requests: acc.requests + (record.requests ?? 1),
            input: acc.input + (record.inputTokens || 0),
            output: acc.output + (record.outputTokens || 0),
        }),
        { requests: 0, input: 0, output: 0 },
    );
}

function bucketSeries(records, byHour) {
    const map = new Map();
    for (const record of records) {
        const iso = record.createdAt || '';
        if (!iso) continue;
        const bucket = byHour ? iso.slice(0, 13) : iso.slice(0, 10);
        const row = map.get(bucket) || { bucket, requests: 0, input: 0, output: 0, tokens: 0 };
        row.requests += record.requests ?? 1;
        row.input += record.inputTokens || 0;
        row.output += record.outputTokens || 0;
        row.tokens += (record.inputTokens || 0) + (record.outputTokens || 0);
        map.set(bucket, row);
    }
    return [...map.values()].sort((a, b) => a.bucket.localeCompare(b.bucket));
}

// The admin analytics endpoint returns camelCase token fields; normalise to the
// short keys the charts use (same shape as the self-mode bucketSeries rows).
function normalizeSeries(rows = []) {
    return rows.map((row) => ({
        bucket: row.bucket,
        requests: row.requests ?? 0,
        tokens: row.tokens ?? 0,
        input: row.input ?? row.inputTokens ?? 0,
        output: row.output ?? row.outputTokens ?? 0,
    }));
}

function formatBucket(key, byHour) {
    if (!key) return '';
    return byHour ? `${key.slice(11)}:00` : key.slice(5);
}

function ValueTooltip({ active, payload, label, unit = '' }) {
    if (!active || !payload?.length) return null;
    return (
        <ChartTooltip title={label}>
            {payload.map((entry) => (
                <ChartTooltipRow key={entry.dataKey} color={entry.color} label={entry.name} value={entry.value} unit={unit} />
            ))}
        </ChartTooltip>
    );
}

function PieCard({ title, data }) {
    const total = data.reduce((sum, slice) => sum + slice.value, 0);
    return (
        <ChartContainer title={title}>
            {total === 0 ? (
                <EmptyState icon={<Activity size={22} />} title="No data" message="No usage in the selected range." />
            ) : (
                <>
                    <div className="relative">
                        <ResponsiveContainer width="100%" height={200}>
                            <PieChart>
                                <Pie data={data} dataKey="value" nameKey="name" innerRadius={60} outerRadius={88} paddingAngle={3} cornerRadius={6} stroke="none" isAnimationActive={false}>
                                    {data.map((slice) => <Cell key={slice.name} fill={slice.color} />)}
                                </Pie>
                                <Tooltip content={<ValueTooltip />} />
                            </PieChart>
                        </ResponsiveContainer>
                        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                            <span className="text-[10px] uppercase tracking-wide text-slate-500">Total</span>
                            <span className="font-mono text-sm font-semibold text-slate-100">{numberFormat.format(total)}</span>
                        </div>
                    </div>
                    <ChartLegend
                        entries={data.map((slice) => ({
                            label: `${slice.name} · ${numberFormat.format(slice.value)} (${Math.round((slice.value / total) * 100)}%)`,
                            color: slice.color,
                        }))}
                        className="mt-4 justify-center"
                    />
                </>
            )}
        </ChartContainer>
    );
}

function TokensBar({ title, data, byHour }) {
    return (
        <ChartContainer title={title}>
            {data.length === 0 ? (
                <EmptyState icon={<Activity size={22} />} title="No data" message="No usage in the selected range." />
            ) : (
                <>
                    <ResponsiveContainer width="100%" height={220}>
                        <BarChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }} barCategoryGap="25%">
                            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                            <XAxis dataKey="bucket" tickFormatter={(value) => formatBucket(value, byHour)} {...AXIS_PROPS} minTickGap={24} />
                            <YAxis {...AXIS_PROPS} width={48} />
                            <Tooltip content={<ValueTooltip />} cursor={{ fill: 'rgba(148,163,184,0.08)' }} />
                            <Bar dataKey="input" name="Input" stackId="tokens" fill={USAGE_COLORS[1]} maxBarSize={80} isAnimationActive={false} />
                            <Bar dataKey="output" name="Output" stackId="tokens" fill={USAGE_COLORS[0]} radius={[6, 6, 0, 0]} maxBarSize={80} isAnimationActive={false} />
                        </BarChart>
                    </ResponsiveContainer>
                    <ChartLegend
                        entries={[
                            { label: 'Input', color: USAGE_COLORS[1] },
                            { label: 'Output', color: USAGE_COLORS[0] },
                        ]}
                        className="mt-3 justify-center"
                    />
                </>
            )}
        </ChartContainer>
    );
}

function RequestsTrend({ title, data, byHour }) {
    return (
        <ChartContainer title={title}>
            {data.length === 0 ? (
                <EmptyState icon={<Activity size={22} />} title="No data" message="No usage in the selected range." />
            ) : (
                <ResponsiveContainer width="100%" height={200}>
                    <AreaChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                        <XAxis dataKey="bucket" tickFormatter={(value) => formatBucket(value, byHour)} {...AXIS_PROPS} minTickGap={24} />
                        <YAxis {...AXIS_PROPS} width={48} allowDecimals={false} />
                        <Tooltip content={<ValueTooltip />} />
                        <Area type="monotone" dataKey="requests" name="API requests" stroke={USAGE_COLORS[1]} fill={USAGE_COLORS[1]} fillOpacity={0.15} strokeWidth={2} isAnimationActive={false} />
                    </AreaChart>
                </ResponsiveContainer>
            )}
        </ChartContainer>
    );
}

// Usage view. `mode="self"` shows the caller's own usage analytics; `mode="admin"`
// shows recent usage across users/models/clusters (gated by usage:report:read).
export function ModelUsagePage({ mode = 'self', onToggleMobileNav }) {
    const [records, setRecords] = useState([]);
    const [tokens, setTokens] = useState([]);
    const [error, setError] = useState('');
    const [range, setRange] = useState('today');
    const [customFrom, setCustomFrom] = useState('');
    const [customTo, setCustomTo] = useState('');
    const [keyFilter, setKeyFilter] = useState('all');
    const [metric, setMetric] = useState('tokens');
    const [analytics, setAnalytics] = useState(null);
    const [adminLoading, setAdminLoading] = useState(mode === 'admin');
    const [groupBy, setGroupBy] = useState('model');
    const [dimFilters, setDimFilters] = useState({ user: new Set(), model: new Set(), cluster: new Set() });
    const loadedOnceRef = useRef(false);

    const load = useCallback(async () => {
        try {
            const [items, tokenItems] = await Promise.all([
                listUsageRecords({ limit: 500 }),
                listModelTokens(),
            ]);
            setRecords(items);
            setTokens(tokenItems);
            setError('');
        } catch (failure) {
            setError(failure?.message || 'Failed to load usage');
        } finally {
            loadedOnceRef.current = true;
        }
    }, []);

    const adminParamsRef = useRef({});
    adminParamsRef.current = { range, customFrom, customTo, groupBy, dimFilters };

    const adminFetch = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet) setAdminLoading(true);
        try {
            const params = adminParamsRef.current;
            const bounds = rangeBounds(params.range, params.customFrom, params.customTo);
            const data = await getUsageAnalytics({
                since: bounds.since ? bounds.since.toISOString() : undefined,
                until: bounds.until ? bounds.until.toISOString() : undefined,
                interval: isHourly(bounds) ? 'hour' : 'day',
                groupBy: params.groupBy,
                userIds: params.dimFilters.user.size ? [...params.dimFilters.user] : undefined,
                groupIds: params.dimFilters.model.size ? [...params.dimFilters.model] : undefined,
                clusterIds: params.dimFilters.cluster.size ? [...params.dimFilters.cluster] : undefined,
            });
            setAnalytics(data);
            setError('');
        } catch (failure) {
            setError(failure?.message || 'Failed to load usage');
        } finally {
            setAdminLoading(false);
        }
    }, []);

    const toggleDim = useCallback((dimension, value) => {
        setDimFilters((current) => {
            const next = new Set(current[dimension]);
            if (value === '') next.clear();
            else if (next.has(value)) next.delete(value);
            else next.add(value);
            return { ...current, [dimension]: next };
        });
    }, []);

    useEffect(() => {
        if (mode !== 'admin') load();
    }, [mode, load]);

    useEffect(() => {
        if (mode === 'admin') adminFetch();
    }, [mode, range, customFrom, customTo, groupBy, dimFilters, adminFetch]);

    usePolling(
        () => (mode === 'admin' ? adminFetch({ quiet: true }) : load()),
        { intervalMs: mode === 'admin' ? 30000 : 5000 },
    );

    const bounds = useMemo(() => rangeBounds(range, customFrom, customTo), [range, customFrom, customTo]);
    const filtered = useMemo(() => records.filter((record) => {
        if (keyFilter !== 'all' && record.tokenId !== keyFilter) return false;
        if (record.createdAt) {
            const at = new Date(record.createdAt).getTime();
            if (bounds.since && at < bounds.since.getTime()) return false;
            if (bounds.until && at > bounds.until.getTime()) return false;
        }
        return true;
    }), [records, keyFilter, bounds]);

    const title = mode === 'admin' ? 'Model usage' : 'My model usage';

    // --- admin: cross-user analytics ----------------------------------------
    if (mode === 'admin') {
        const data = analytics;
        const totals = data?.totals || { requests: 0, inputTokens: 0, cachedInputTokens: 0, outputTokens: 0, tokens: 0 };
        const composition = COMPOSITION
            .map((slice) => ({
                ...slice,
                value: slice.key === 'input' ? totals.inputTokens : totals.outputTokens,
            }))
            .filter((slice) => slice.value > 0);
        const groups = (data?.groups || []).map((group) => ({ ...group, series: normalizeSeries(group.series) }));
        const dimensionPie = groups.slice(0, 4).map((group, index) => ({
            name: group.label, value: group.totals.tokens, color: USAGE_COLORS[index],
        }));
        const rest = groups.slice(4).reduce((sum, group) => sum + group.totals.tokens, 0);
        if (rest > 0) dimensionPie.push({ name: 'Other', value: rest, color: OTHER_COLOR });
        const series = normalizeSeries(data?.series);
        const byHour = (data?.interval || 'day') === 'hour';
        const facets = data?.facets || { models: [], users: [], clusters: [] };
        const dimensionLabel = groupBy === 'user' ? 'user' : groupBy === 'cluster' ? 'cluster' : 'model';

        return (
            <ModulePage>
                <div className="flex w-full flex-col gap-6">
                    <ModuleHeader
                        icon={Activity}
                        title={title}
                        description="Cross-user token consumption by model, user or cluster. Counts only — no billing in this version."
                        onToggleMobileNav={onToggleMobileNav}
                    />

                    {error && (
                        <p role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">{error}</p>
                    )}

                    <section className={cn(CARD, 'overflow-visible p-6')}>
                        <div className="flex flex-wrap items-end gap-3">
                            <label className="text-xs text-slate-400">
                                Time range
                                <Select value={range} onChange={(event) => setRange(event.target.value)} className="mt-1">
                                    {RANGE_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                                </Select>
                            </label>
                            {range === 'custom' && (
                                <>
                                    <label className="text-xs text-slate-400">
                                        Start date
                                        <Input type="date" value={customFrom} onChange={(event) => setCustomFrom(event.target.value)} className="mt-1" />
                                    </label>
                                    <label className="text-xs text-slate-400">
                                        End date
                                        <Input type="date" value={customTo} onChange={(event) => setCustomTo(event.target.value)} className="mt-1" />
                                    </label>
                                </>
                            )}
                            <div className="text-xs text-slate-400">
                                Group by
                                <ToggleGroup
                                    className="mt-1"
                                    options={[
                                        { value: 'model', label: 'Model' },
                                        { value: 'user', label: 'User' },
                                        { value: 'cluster', label: 'Cluster' },
                                    ]}
                                    value={groupBy}
                                    onChange={setGroupBy}
                                />
                            </div>
                            <MultiSelectDropdown
                                className="w-44"
                                label="User"
                                emptyLabel="All users"
                                clearLabel="All users"
                                searchable
                                searchPlaceholder="Search users…"
                                options={facets.users.map((option) => option.key)}
                                formatLabel={(key) => facets.users.find((option) => option.key === key)?.label || key}
                                selected={dimFilters.user}
                                onChange={(value) => toggleDim('user', value)}
                            />
                            <MultiSelectDropdown
                                className="w-44"
                                label="Model"
                                emptyLabel="All models"
                                clearLabel="All models"
                                searchable
                                searchPlaceholder="Search models…"
                                options={facets.models.map((option) => option.key)}
                                formatLabel={(key) => facets.models.find((option) => option.key === key)?.label || key}
                                selected={dimFilters.model}
                                onChange={(value) => toggleDim('model', value)}
                            />
                            <MultiSelectDropdown
                                className="w-44"
                                label="Cluster"
                                emptyLabel="All clusters"
                                clearLabel="All clusters"
                                searchable
                                searchPlaceholder="Search clusters…"
                                options={facets.clusters.map((option) => option.key)}
                                formatLabel={(key) => facets.clusters.find((option) => option.key === key)?.label || key}
                                selected={dimFilters.cluster}
                                onChange={(value) => toggleDim('cluster', value)}
                            />
                            <Button
                                variant="secondary"
                                size="sm"
                                className="ml-auto"
                                onClick={() => { setRange('today'); setCustomFrom(''); setCustomTo(''); setGroupBy('model'); setDimFilters({ user: new Set(), model: new Set(), cluster: new Set() }); setMetric('tokens'); }}
                            >
                                Clear filters
                            </Button>
                        </div>
                        <div className="mt-4 grid grid-cols-2 gap-4">
                            <StatCard title="API requests" value={numberFormat.format(totals.requests)} />
                            <StatCard title="Tokens" value={numberFormat.format(totals.tokens)} />
                        </div>
                    </section>

                    {!data ? (
                        <AsyncState loading={adminLoading} error={null} empty={false} emptyContent={null}>
                            <div />
                        </AsyncState>
                    ) : (
                        <>
                            <div className="grid gap-4 lg:grid-cols-2">
                                <PieCard title="Token composition" data={composition} />
                                <PieCard title={`Tokens by ${dimensionLabel}`} data={dimensionPie} />
                            </div>

                            <ChartContainer title="Tokens over time" actions={<ToggleGroup options={METRICS} value={metric} onChange={setMetric} />}>
                                {series.length === 0 ? (
                                    <EmptyState icon={<Activity size={22} />} title="No usage" message="No usage in the selected range." />
                                ) : (
                                    <ResponsiveContainer width="100%" height={260}>
                                        <BarChart data={series} margin={{ top: 4, right: 8, left: 0, bottom: 0 }} barCategoryGap="25%">
                                            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                                            <XAxis dataKey="bucket" tickFormatter={(value) => formatBucket(value, byHour)} {...AXIS_PROPS} minTickGap={24} />
                                            <YAxis {...AXIS_PROPS} width={52} />
                                            <Tooltip content={<ValueTooltip />} cursor={{ fill: 'rgba(148,163,184,0.08)' }} />
                                            <Bar dataKey={metric} name={METRICS.find((item) => item.value === metric)?.label || metric} fill={METRIC_COLOR[metric]} radius={[6, 6, 0, 0]} maxBarSize={80} isAnimationActive={false} />
                                        </BarChart>
                                    </ResponsiveContainer>
                                )}
                            </ChartContainer>

                            {groups.map((group) => (
                                <div key={group.key} className="flex flex-col gap-4">
                                    <SectionLabel>{group.label}</SectionLabel>
                                    <div className="grid gap-4 lg:grid-cols-2">
                                        <TokensBar title={`${group.label} · tokens`} data={group.series} byHour={byHour} />
                                        <RequestsTrend title={`${group.label} · API requests`} data={group.series} byHour={byHour} />
                                    </div>
                                </div>
                            ))}
                        </>
                    )}
                </div>
            </ModulePage>
        );
    }


    // --- self: analytics ------------------------------------------------------
    const totals = totalsOf(filtered);
    const totalTokens = totals.input + totals.output;
    const composition = COMPOSITION.map((slice) => ({ ...slice, value: totals[slice.key] })).filter((slice) => slice.value > 0);

    const byModel = new Map();
    for (const record of filtered) {
        const model = record.groupName || record.modelRef || 'unknown';
        byModel.set(model, (byModel.get(model) || 0)
            + (record.inputTokens || 0) + (record.outputTokens || 0));
    }
    const modelEntries = [...byModel.entries()].sort((a, b) => b[1] - a[1]);
    const modelPie = modelEntries.slice(0, 4).map(([name, value], index) => ({ name, value, color: USAGE_COLORS[index] }));
    const rest = modelEntries.slice(4).reduce((sum, [, value]) => sum + value, 0);
    if (rest > 0) modelPie.push({ name: 'Other', value: rest, color: OTHER_COLOR });

    const byHour = isHourly(bounds);
    const overallDaily = bucketSeries(filtered, byHour);
    const modelNames = modelEntries.map(([name]) => name);

    return (
        <ModulePage>
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={Activity}
                    title={title}
                    description="Token consumption by model and API key. Counts only — no billing in this version."
                    onToggleMobileNav={onToggleMobileNav}
                />

                {error && (
                    <p role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">{error}</p>
                )}

                <section className={cn(CARD, 'p-6')}>
                    <div className="flex flex-wrap items-end gap-3">
                        <label className="text-xs text-slate-400">
                            Time range
                            <Select value={range} onChange={(event) => setRange(event.target.value)} className="mt-1">
                                {RANGE_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                            </Select>
                        </label>
                        {range === 'custom' && (
                            <>
                                <label className="text-xs text-slate-400">
                                    Start date
                                    <Input type="date" value={customFrom} onChange={(event) => setCustomFrom(event.target.value)} className="mt-1" />
                                </label>
                                <label className="text-xs text-slate-400">
                                    End date
                                    <Input type="date" value={customTo} onChange={(event) => setCustomTo(event.target.value)} className="mt-1" />
                                </label>
                            </>
                        )}
                        <label className="text-xs text-slate-400">
                            API key
                            <Select value={keyFilter} onChange={(event) => setKeyFilter(event.target.value)} className="mt-1">
                                <option value="all">All keys</option>
                                {tokens.map((token) => (
                                    <option key={token.id} value={token.id}>{token.name} ({maskedKeyHint(token.tokenHint)})</option>
                                ))}
                            </Select>
                        </label>
                        <Button
                            variant="secondary"
                            size="sm"
                            className="ml-auto"
                            onClick={() => { setRange('today'); setCustomFrom(''); setCustomTo(''); setKeyFilter('all'); setMetric('tokens'); }}
                        >
                            Clear filters
                        </Button>
                    </div>
                    <div className="mt-4 grid grid-cols-2 gap-4">
                        <StatCard title="API requests" value={numberFormat.format(totals.requests)} />
                        <StatCard title="Tokens" value={numberFormat.format(totalTokens)} />
                    </div>
                </section>

                <div className="grid gap-4 lg:grid-cols-2">
                    <PieCard title="Token composition" data={composition} />
                    <PieCard title="Tokens by model" data={modelPie} />
                </div>

                <ChartContainer
                    title="Tokens over time"
                    actions={<ToggleGroup options={METRICS} value={metric} onChange={setMetric} />}
                >
                    {overallDaily.length === 0 ? (
                        <EmptyState icon={<Activity size={22} />} title="No usage yet" message="Usage appears here after your first model call." />
                    ) : (
                        <ResponsiveContainer width="100%" height={260}>
                            <BarChart data={overallDaily} margin={{ top: 4, right: 8, left: 0, bottom: 0 }} barCategoryGap="25%">
                                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                                <XAxis dataKey="bucket" tickFormatter={(value) => formatBucket(value, byHour)} {...AXIS_PROPS} minTickGap={24} />
                                <YAxis {...AXIS_PROPS} width={52} />
                                <Tooltip content={<ValueTooltip />} cursor={{ fill: 'rgba(148,163,184,0.08)' }} />
                                <Bar
                                    dataKey={metric}
                                    name={METRICS.find((item) => item.value === metric)?.label || metric}
                                    fill={METRIC_COLOR[metric]}
                                    radius={[6, 6, 0, 0]}
                                    maxBarSize={80}
                                    isAnimationActive={false}
                                />
                            </BarChart>
                        </ResponsiveContainer>
                    )}
                </ChartContainer>

                {modelNames.map((model) => {
                    const modelDaily = bucketSeries(filtered.filter((record) => (record.groupName || record.modelRef || 'unknown') === model), byHour);
                    return (
                        <div key={model} className="flex flex-col gap-4">
                            <SectionLabel>{model}</SectionLabel>
                            <div className="grid gap-4 lg:grid-cols-2">
                                <TokensBar title={`${model} · tokens`} data={modelDaily} byHour={byHour} />
                                <RequestsTrend title={`${model} · API requests`} data={modelDaily} byHour={byHour} />
                            </div>
                        </div>
                    );
                })}
            </div>
        </ModulePage>
    );
}

export default ModelUsagePage;
