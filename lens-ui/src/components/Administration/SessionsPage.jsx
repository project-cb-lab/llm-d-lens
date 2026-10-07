import { useCallback, useEffect, useRef, useState } from 'react';
import { usePolling } from '../../hooks/usePolling';
import { Monitor, RefreshCw, Trash2 } from 'lucide-react';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { Button } from '../ui/Button';
import { Badge } from '../ui/Badge';
import { AsyncState } from '../shared/AsyncState';
import { EmptyState } from '../ui/EmptyState';
import { useNotice } from '../../hooks/useNotice';
import { errorMessage } from '../../utils/errorMessage';
import { PermissionGate } from '../../features/auth/PermissionGate';
import { listSessions, revokeSession } from '../../features/auth/adminClient';
import { formatTimestamp } from '../../utils/formatTimestamp';
import { AdminPanel } from './AdminPanel';
import { AdminAlert } from './AdminAlert';

export default function SessionsPage({ onToggleMobileNav }) {
    const [sessions, setSessions] = useState([]);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useNotice();
    const [revokingId, setRevokingId] = useState(null);
    const loadedOnce = useRef(false);

    const load = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet && loadedOnce.current) setRefreshing(true);
        try {
            setSessions(await listSessions());
            setError('');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to load sessions'));
        } finally {
            loadedOnce.current = true;
            setLoading(false);
            setRefreshing(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);
    usePolling(() => load({ quiet: true }));

    const handleRevoke = async (session) => {
        setRevokingId(session.id);
        try {
            await revokeSession(session.id);
            await load();
            setNotice('Session revoked');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to revoke session'));
        } finally {
            setRevokingId(null);
        }
    };

    return (
        <ModulePage>
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={Monitor}
                    title="Active sessions"
                    description="Sessions signed in with your account. Revoke any you do not recognize."
                    onToggleMobileNav={onToggleMobileNav}
                    actions={
                        <Button variant="secondary" size="sm" onClick={load} isLoading={refreshing} disabled={loading}>
                            <RefreshCw size={14} /> Refresh
                        </Button>
                    }
                />

                <AdminAlert tone="success">{notice}</AdminAlert>
                {error && sessions.length > 0 && <AdminAlert>{error}</AdminAlert>}

                <AsyncState
                    loading={loading}
                    error={sessions.length === 0 ? error : null}
                    empty={sessions.length === 0}
                    onRetry={load}
                    emptyContent={<EmptyState icon={<Monitor size={22} />} title="No active sessions" message="You have no other active sessions." />}
                >
                    <AdminPanel icon={Monitor} title="Your sessions" count={sessions.length}>
                        <div className="overflow-x-auto">
                            <table className="w-full min-w-[44rem] text-left text-sm">
                                <thead className="bg-slate-950/60 text-xs uppercase tracking-wide text-slate-500">
                                    <tr>
                                        <th className="px-4 py-3">User</th>
                                        <th className="px-4 py-3">IP</th>
                                        <th className="px-4 py-3">Source</th>
                                        <th className="px-4 py-3">Created</th>
                                        <th className="px-4 py-3">Last seen</th>
                                        <th className="px-4 py-3">Expires</th>
                                        <th className="px-4 py-3 text-right">Actions</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {sessions.map((session) => (
                                        <tr key={session.id} className="border-t border-slate-800">
                                            <td className="px-4 py-3 font-medium text-slate-200">{session.username || '—'}</td>
                                            <td className="px-4 py-3 font-mono text-xs text-slate-300">{session.ip || '—'}</td>
                                            <td className="px-4 py-3"><Badge tone="neutral">{session.auth_source}</Badge></td>
                                            <td className="px-4 py-3 text-slate-400">{formatTimestamp(session.created_at)}</td>
                                            <td className="px-4 py-3 text-slate-400">{formatTimestamp(session.last_seen_at)}</td>
                                            <td className="px-4 py-3 text-slate-400">{formatTimestamp(session.expires_at)}</td>
                                            <td className="px-4 py-3">
                                                <div className="flex justify-end">
                                                    <PermissionGate permission="session:session:read">
                                                        <Button
                                                            variant="ghost"
                                                            size="icon"
                                                            onClick={() => handleRevoke(session)}
                                                            isLoading={revokingId === session.id}
                                                            aria-label="Revoke session"
                                                        >
                                                            <Trash2 size={14} className="text-rose-400" />
                                                        </Button>
                                                    </PermissionGate>
                                                </div>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </AdminPanel>
                </AsyncState>
            </div>
        </ModulePage>
    );
}
