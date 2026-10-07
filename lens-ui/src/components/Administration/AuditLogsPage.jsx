import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { usePolling } from '../../hooks/usePolling';
import { RefreshCw, ScrollText } from 'lucide-react';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { Button } from '../ui/Button';
import { Input, Select } from '../ui/FormControls';
import { Badge } from '../ui/Badge';
import { AsyncState } from '../shared/AsyncState';
import { EmptyState } from '../ui/EmptyState';
import { PaginationControls } from '../ui/PaginationControls';
import { errorMessage } from '../../utils/errorMessage';
import { listAuditLogs, listUsers } from '../../features/auth/adminClient';
import { formatTimestamp } from '../../utils/formatTimestamp';
import { AdminClearButton, AdminPanel, AdminToolbar } from './AdminPanel';
import { AdminAlert } from './AdminAlert';

const EVENT_TONES = {
    login: 'success',
    logout: 'neutral',
    login_failed: 'danger',
    access_denied: 'warning',
    mutation: 'info',
    admin: 'violet',
    bootstrap: 'brand',
    password_changed: 'info',
    password_reset: 'warning',
    user_created: 'info',
    user_updated: 'info',
    user_deleted: 'danger',
    session_revoked: 'warning',
    directory_synced: 'info',
};

// ``result`` values the audit writers use (success/failure/deny).
const RESULT_OPTIONS = ['success', 'failure', 'deny'];
const PAGE_SIZE_OPTIONS = [25, 50, 100, 200];

function toIso(value) {
    if (!value) return undefined;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

export default function AuditLogsPage({ onToggleMobileNav }) {
    const [logs, setLogs] = useState([]);
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState('');
    const [eventType, setEventType] = useState('');
    const [result, setResult] = useState('');
    const [actorText, setActorText] = useState('');
    const [actorUserId, setActorUserId] = useState('');
    const [since, setSince] = useState('');
    const [until, setUntil] = useState('');
    const [page, setPage] = useState(0);
    const [pageSize, setPageSize] = useState(50);
    const [users, setUsers] = useState([]);
    const loadedOnce = useRef(false);

    const load = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet && loadedOnce.current) setRefreshing(true);
        try {
            const payload = await listAuditLogs({
                eventType: eventType || undefined,
                result: result || undefined,
                actorUserId: actorUserId || undefined,
                since: toIso(since),
                until: toIso(until),
                limit: pageSize,
                offset: page * pageSize,
            });
            setLogs(Array.isArray(payload?.items) ? payload.items : []);
            setTotal(Number(payload?.total) || 0);
            setError('');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to load audit log'));
        } finally {
            loadedOnce.current = true;
            setLoading(false);
            setRefreshing(false);
        }
    }, [eventType, result, actorUserId, since, until, page, pageSize]);

    useEffect(() => {
        load();
    }, [load]);
    usePolling(() => load({ quiet: true }));

    // Actor filter: a searchable list of known actors (native datalist search).
    useEffect(() => {
        listUsers()
            .then((items) => setUsers(Array.isArray(items) ? items : []))
            .catch(() => setUsers([]));
    }, []);

    const eventOptions = useMemo(
        () => [...new Set([...Object.keys(EVENT_TONES), ...logs.map((log) => log.event_type)])].sort(),
        [logs]
    );

    const hasFilters = Boolean(eventType || result || actorUserId || since || until);
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const pageStart = total === 0 ? 0 : page * pageSize + 1;
    const pageEnd = Math.min(total, page * pageSize + logs.length);

    const update = (setter) => (value) => {
        setter(value);
        setPage(0);
    };

    const onActorChange = (value) => {
        setActorText(value);
        const match = users.find((user) => user.username === value);
        setActorUserId(match?.id || '');
        setPage(0);
    };

    const clearFilters = () => {
        setEventType('');
        setResult('');
        setActorText('');
        setActorUserId('');
        setSince('');
        setUntil('');
        setPage(0);
    };

    return (
        <ModulePage>
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={ScrollText}
                    title="Audit log"
                    description="Authentication and authorization events, newest first."
                    onToggleMobileNav={onToggleMobileNav}
                    actions={
                        <Button variant="secondary" size="sm" onClick={load} isLoading={refreshing} disabled={loading}>
                            <RefreshCw size={14} /> Refresh
                        </Button>
                    }
                />

                {error && logs.length > 0 && <AdminAlert>{error}</AdminAlert>}

                <AsyncState
                    loading={loading}
                    error={logs.length === 0 ? error : null}
                    empty={logs.length === 0 && !hasFilters}
                    onRetry={load}
                    emptyContent={<EmptyState icon={<ScrollText size={22} />} title="No events" message="Authentication and authorization events appear here." />}
                >
                    <AdminPanel
                        icon={ScrollText}
                        title="Events"
                        count={total}
                        toolbar={
                            <AdminToolbar>
                                <div className="w-48 shrink-0">
                                    <Select value={eventType} onChange={(event) => update(setEventType)(event.target.value)} aria-label="Event filter">
                                        <option value="">All events</option>
                                        {eventOptions.map((event) => (
                                            <option key={event} value={event}>{event}</option>
                                        ))}
                                    </Select>
                                </div>
                                <div className="w-40 shrink-0">
                                    <Select value={result} onChange={(event) => update(setResult)(event.target.value)} aria-label="Result filter">
                                        <option value="">All results</option>
                                        {RESULT_OPTIONS.map((value) => (
                                            <option key={value} value={value}>{value}</option>
                                        ))}
                                    </Select>
                                </div>
                                <div className="w-56 shrink-0">
                                    <Input
                                        list="audit-actor-options"
                                        value={actorText}
                                        onChange={(event) => onActorChange(event.target.value)}
                                        placeholder="Filter by user…"
                                        aria-label="Actor filter"
                                    />
                                    <datalist id="audit-actor-options">
                                        {users.map((user) => <option key={user.id} value={user.username} />)}
                                    </datalist>
                                </div>
                                <label className="flex shrink-0 items-center gap-2 text-xs text-slate-400">
                                    <span className="whitespace-nowrap">Start</span>
                                    <Input
                                        type="datetime-local"
                                        value={since}
                                        max={until || undefined}
                                        onChange={(event) => update(setSince)(event.target.value)}
                                        aria-label="Start time"
                                        title="Start time"
                                        className="w-52"
                                    />
                                </label>
                                <label className="flex shrink-0 items-center gap-2 text-xs text-slate-400">
                                    <span className="whitespace-nowrap">End</span>
                                    <Input
                                        type="datetime-local"
                                        value={until}
                                        min={since || undefined}
                                        onChange={(event) => update(setUntil)(event.target.value)}
                                        aria-label="End time"
                                        title="End time"
                                        className="w-52"
                                    />
                                </label>
                                <AdminClearButton show={hasFilters} onClick={clearFilters} />
                            </AdminToolbar>
                        }
                    >
                        {logs.length === 0 ? (
                            <EmptyState title="No matching events" />
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full min-w-[48rem] text-left text-sm">
                                    <thead className="bg-slate-950/60 text-xs uppercase tracking-wide text-slate-500">
                                        <tr>
                                            <th className="px-4 py-3">Time</th>
                                            <th className="px-4 py-3">Event</th>
                                            <th className="px-4 py-3">Actor</th>
                                            <th className="px-4 py-3">Result</th>
                                            <th className="px-4 py-3">Detail</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {logs.map((log) => (
                                            <tr key={log.id} className="border-t border-slate-800">
                                                <td className="whitespace-nowrap px-4 py-3 text-slate-400">{formatTimestamp(log.created_at)}</td>
                                                <td className="px-4 py-3">
                                                    <Badge tone={EVENT_TONES[log.event_type] || 'neutral'}>{log.event_type}</Badge>
                                                </td>
                                                <td className="px-4 py-3 text-slate-300">{log.actor_username || '—'}</td>
                                                <td className="px-4 py-3 text-slate-400">{log.result}</td>
                                                <td className="px-4 py-3 font-mono text-xs text-slate-500">
                                                    {log.permission || log.path || log.target_id || '—'}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                        <div className="mt-2 flex flex-col gap-3 border-t border-slate-800/60 px-1 pt-4 text-xs text-slate-400 sm:flex-row sm:items-center sm:justify-between">
                            <span>Showing {pageStart}–{pageEnd} of {total}</span>
                            <div className="flex items-center gap-2">
                                <label htmlFor="audit-page-size" className="text-slate-500">Rows per page</label>
                                <Select
                                    id="audit-page-size"
                                    className="h-8 w-20 text-xs"
                                    value={String(pageSize)}
                                    onChange={(event) => { setPageSize(Number(event.target.value)); setPage(0); }}
                                >
                                    {PAGE_SIZE_OPTIONS.map((size) => <option key={size} value={size}>{size}</option>)}
                                </Select>
                                <PaginationControls page={page} totalPages={totalPages} onPageChange={setPage} />
                            </div>
                        </div>
                    </AdminPanel>
                </AsyncState>
            </div>
        </ModulePage>
    );
}
