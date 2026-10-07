import { useCallback, useEffect, useRef, useState } from 'react';
import { usePolling } from '../../hooks/usePolling';
import { Plus, RefreshCw, Search, ShieldCheck, Trash2, UserCog } from 'lucide-react';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { Button } from '../ui/Button';
import { Modal } from '../ui/Modal';
import { FormError } from '../ui/FormError';
import { Checkbox, Input, Label, Select } from '../ui/FormControls';
import { Badge } from '../ui/Badge';
import { AsyncState } from '../shared/AsyncState';
import { EmptyState } from '../ui/EmptyState';
import { Spinner } from '../ui/Spinner';
import { useNotice } from '../../hooks/useNotice';
import { useSubmission } from '../../hooks/useSubmission';
import { errorMessage } from '../../utils/errorMessage';
import { PermissionGate } from '../../features/auth/PermissionGate';
import { useAuth } from '../../features/auth/useAuth';
import {
    addGroupRoleBinding,
    createGroup,
    deleteGroup,
    listClusters,
    listGroupMembers,
    listGroups,
    listRoles,
    listUsers,
    setGroupMembers,
} from '../../features/auth/adminClient';
import { AdminClearButton, AdminPanel, AdminSearch, AdminToolbar } from './AdminPanel';
import { AdminAlert } from './AdminAlert';
import { ConfirmDeleteModal } from './ConfirmDeleteModal';
import { RoleBindingsDrawer } from './RoleBindingsDrawer';

function roleScopeLabel(role) {
    if (role.scopeType === 'cluster') return `Cluster: ${role.scopeClusterId || '—'}`;
    if (role.scopeType === 'resource') return `Resource: ${role.scopeResourceType || '—'}:${role.scopeResourceId || '—'}`;
    return 'All clusters';
}

export default function GroupsPage({ onToggleMobileNav }) {
    const [groups, setGroups] = useState([]);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useNotice();
    const [query, setQuery] = useState('');
    const [source, setSource] = useState('');
    const [dialog, setDialog] = useState(null);
    const [pendingDelete, setPendingDelete] = useState(null);
    const [pendingMembers, setPendingMembers] = useState(null);
    const [pendingRoles, setPendingRoles] = useState(null);
    const loadedOnce = useRef(false);

    const load = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet && loadedOnce.current) setRefreshing(true);
        try {
            setGroups(await listGroups());
            setError('');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to load groups'));
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

    const needle = query.trim().toLowerCase();
    const filtered = groups.filter((group) => {
        if (source && group.source !== source) return false;
        if (!needle) return true;
        return [group.name, group.description].filter(Boolean).join(' ').toLowerCase().includes(needle);
    });

    const confirmDelete = async () => {
        await deleteGroup(pendingDelete.id);
        setPendingDelete(null);
        await load();
        setNotice(`Deleted ${pendingDelete.name}`);
    };

    return (
        <ModulePage>
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={UserCog}
                    title="Groups"
                    description="Organize users for bulk role grants and team ownership."
                    onToggleMobileNav={onToggleMobileNav}
                    actions={
                        <>
                            <Button variant="secondary" size="sm" onClick={load} isLoading={refreshing} disabled={loading}>
                                <RefreshCw size={14} /> Refresh
                            </Button>
                            <PermissionGate permission="group:group:create">
                                <Button variant="sky" size="sm" onClick={() => setDialog({ mode: 'add' })}>
                                    <Plus size={14} /> Add group
                                </Button>
                            </PermissionGate>
                        </>
                    }
                />

                <AdminAlert tone="success">{notice}</AdminAlert>
                {error && groups.length > 0 && <AdminAlert>{error}</AdminAlert>}

                <AsyncState
                    loading={loading}
                    error={groups.length === 0 ? error : null}
                    empty={groups.length === 0}
                    onRetry={load}
                    emptyContent={
                        <EmptyState
                            icon={<UserCog size={22} />}
                            title="No groups yet"
                            message="Create a group to grant roles and own resources as a team."
                            action={<PermissionGate permission="group:group:create"><Button variant="sky" size="sm" onClick={() => setDialog({ mode: 'add' })}><Plus size={14} /> Add group</Button></PermissionGate>}
                        />
                    }
                >
                    <AdminPanel
                        icon={UserCog}
                        title="Groups"
                        count={filtered.length}
                        toolbar={
                            <AdminToolbar>
                                <AdminSearch value={query} onChange={setQuery} placeholder="Search group name or description..." />
                                <div className="w-40 shrink-0">
                                    <select
                                        value={source}
                                        onChange={(event) => setSource(event.target.value)}
                                        aria-label="Source filter"
                                        className="h-9 w-full rounded-xl border border-slate-800/60 bg-[#0b0f17] px-3 text-xs text-slate-200 outline-none focus:border-cyan-500/40"
                                    >
                                        <option value="">All sources</option>
                                        <option value="local">Local</option>
                                        <option value="ldap">LDAP</option>
                                    </select>
                                </div>
                                <AdminClearButton show={Boolean(query || source)} onClick={() => { setQuery(''); setSource(''); }} />
                            </AdminToolbar>
                        }
                    >
                        {filtered.length === 0 ? (
                            <EmptyState title="No matching groups" message="Adjust the filters to see more." />
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full min-w-[40rem] text-left text-sm">
                                    <thead className="bg-slate-950/60 text-xs uppercase tracking-wide text-slate-500">
                                        <tr>
                                            <th className="px-4 py-3">Name</th>
                                            <th className="px-4 py-3">Description</th>
                                            <th className="px-4 py-3">Source</th>
                                            <th className="px-4 py-3">Roles</th>
                                            <th className="px-4 py-3 text-right">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {filtered.map((group) => (
                                            <tr key={group.id} className="border-t border-slate-800">
                                                <td className="px-4 py-3 font-medium text-slate-100">{group.name}</td>
                                                <td className="px-4 py-3 text-slate-400">{group.description || '—'}</td>
                                                <td className="px-4 py-3">
                                                    <Badge tone={group.source === 'local' ? 'neutral' : 'info'}>{group.source}</Badge>
                                                </td>
                                                <td className="px-4 py-3">
                                                    {(group.roles || []).length === 0 ? (
                                                        <span className="text-slate-600">—</span>
                                                    ) : (
                                                        <div className="flex flex-wrap gap-1">
                                                            {group.roles.map((role, index) => (
                                                                <Badge key={`${role.name}-${index}`} tone="violet" title={roleScopeLabel(role)}>
                                                                    {role.name}
                                                                </Badge>
                                                            ))}
                                                        </div>
                                                    )}
                                                </td>
                                                <td className="px-4 py-3">
                                                    <div className="flex items-center justify-end gap-1">
                                                        <PermissionGate permission="role:binding:manage">
                                                            <Button variant="ghost" size="icon" onClick={() => setPendingRoles(group)} aria-label={`Roles for ${group.name}`} title="Roles">
                                                                <ShieldCheck size={14} className="text-sky-400" />
                                                            </Button>
                                                        </PermissionGate>
                                                        <PermissionGate permission="group:group:manage-members">
                                                            <span title={group.source !== 'local' ? 'Managed by IdP' : undefined}>
                                                                <Button
                                                                    variant="secondary"
                                                                    size="xs"
                                                                    onClick={() => setPendingMembers(group)}
                                                                    disabled={group.source !== 'local'}
                                                                >
                                                                    Members
                                                                </Button>
                                                            </span>
                                                        </PermissionGate>
                                                        <PermissionGate permission="group:group:delete">
                                                            <Button variant="ghost" size="icon" onClick={() => setPendingDelete(group)} aria-label={`Delete ${group.name}`}>
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
                        )}
                    </AdminPanel>
                </AsyncState>
            </div>

            {dialog?.mode === 'add' && (
                <GroupFormModal onCancel={() => setDialog(null)} onSaved={() => { setDialog(null); load(); setNotice('Group created'); }} />
            )}
            {pendingRoles && (
                <RoleBindingsDrawer
                    subjectType="group"
                    subjectId={pendingRoles.id}
                    subjectLabel={pendingRoles.name}
                    onClose={() => setPendingRoles(null)}
                />
            )}
            {pendingMembers && (
                <GroupMembersModal
                    group={pendingMembers}
                    onCancel={() => setPendingMembers(null)}
                    onSaved={() => { setPendingMembers(null); setNotice('Members updated'); }}
                />
            )}
            {pendingDelete && (
                <ConfirmDeleteModal
                    title="Delete group"
                    subtitle={pendingDelete.name}
                    warning="Members lose the roles granted through this group. Users and other grants are unaffected."
                    confirmLabel="Delete group"
                    onCancel={() => setPendingDelete(null)}
                    onDelete={confirmDelete}
                />
            )}
        </ModulePage>
    );
}

function GroupFormModal({ onCancel, onSaved }) {
    const { can } = useAuth();
    const mayBind = can('role:binding:manage');
    const [form, setForm] = useState({ name: '', description: '' });
    const [roles, setRoles] = useState([]);
    const [clusters, setClusters] = useState([]);
    const [selectedRoles, setSelectedRoles] = useState(new Set());
    const [scopeType, setScopeType] = useState('global');
    const [clusterId, setClusterId] = useState('');
    const { pending, error, setError, run } = useSubmission('Failed to create group', { keepPendingOnSuccess: true });
    const nameRef = useRef(null);
    useEffect(() => nameRef.current?.focus(), []);
    useEffect(() => {
        if (!mayBind) return undefined;
        let active = true;
        Promise.all([listRoles(), listClusters()])
            .then(([roleList, clusterResponse]) => {
                if (!active) return;
                setRoles(roleList || []);
                setClusters(clusterResponse?.items || []);
            })
            .catch(() => {});
        return () => { active = false; };
    }, [mayBind]);

    const toggleRole = (roleId) => setSelectedRoles((current) => {
        const next = new Set(current);
        if (next.has(roleId)) next.delete(roleId); else next.add(roleId);
        return next;
    });

    const submit = async (event) => {
        event.preventDefault();
        if (pending) return;
        if (mayBind && selectedRoles.size > 0 && scopeType === 'cluster' && !clusterId) {
            setError('Select a cluster for the chosen scope.');
            return;
        }
        await run(async () => {
            const group = await createGroup({ name: form.name.trim(), description: form.description.trim() });
            if (mayBind && selectedRoles.size > 0) {
                for (const roleId of selectedRoles) {
                    await addGroupRoleBinding(group.id, {
                        roleId,
                        scopeType,
                        scopeClusterId: scopeType === 'cluster' ? clusterId : null,
                    });
                }
            }
            onSaved();
        });
    };

    return (
        <Modal
            isOpen
            onClose={pending ? undefined : onCancel}
            title="Add group"
            variant="drawer"
            size="md"
            closeOnBackdrop={!pending}
            closeOnEscape={!pending}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={pending}>Cancel</Button>
                    <Button type="submit" form="admin-group-form" variant="primary" isLoading={pending}>Add group</Button>
                </>
            }
        >
            <form id="admin-group-form" onSubmit={submit} className="flex flex-col gap-4">
                <div>
                    <Label htmlFor="admin-group-name">Name</Label>
                    <Input ref={nameRef} id="admin-group-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="e.g. platform-team" required maxLength={150} />
                </div>
                <div>
                    <Label htmlFor="admin-group-description">Description</Label>
                    <Input id="admin-group-description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="Optional" />
                </div>
                {mayBind && (
                    <div className="flex flex-col gap-3 rounded-xl border border-theme-border p-3">
                        <div>
                            <Label>Roles</Label>
                            {roles.length === 0 ? (
                                <p className="text-xs text-theme-muted">No roles available.</p>
                            ) : (
                                <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                                    {roles.map((role) => (
                                        <Checkbox
                                            key={role.id}
                                            checked={selectedRoles.has(role.id)}
                                            onChange={() => toggleRole(role.id)}
                                            label={<span className="text-xs">{role.name}</span>}
                                        />
                                    ))}
                                </div>
                            )}
                        </div>
                        {selectedRoles.size > 0 && (
                            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                                <div>
                                    <Label htmlFor="admin-group-scope">Scope</Label>
                                    <Select id="admin-group-scope" value={scopeType} onChange={(event) => setScopeType(event.target.value)}>
                                        <option value="global">All clusters (global)</option>
                                        <option value="cluster">Specific cluster</option>
                                    </Select>
                                </div>
                                {scopeType === 'cluster' && (
                                    <div>
                                        <Label htmlFor="admin-group-cluster">Cluster</Label>
                                        <Select id="admin-group-cluster" value={clusterId} onChange={(event) => setClusterId(event.target.value)}>
                                            <option value="">Select a cluster…</option>
                                            {clusters.map((cluster) => (
                                                <option key={cluster.id} value={cluster.id}>{cluster.name || cluster.id}</option>
                                            ))}
                                        </Select>
                                    </div>
                                )}
                            </div>
                        )}
                        <p className="text-[11px] text-theme-muted">
                            Optional — you can also assign roles later from the group's Roles drawer.
                        </p>
                    </div>
                )}
                <FormError message={error} />
            </form>
        </Modal>
    );
}

function GroupMembersModal({ group, onCancel, onSaved }) {
    const [users, setUsers] = useState([]);
    const [selected, setSelected] = useState(new Set());
    const [loading, setLoading] = useState(true);
    const [loadError, setLoadError] = useState('');
    const [query, setQuery] = useState('');
    const [selectedOnly, setSelectedOnly] = useState(false);
    const { pending, error, run } = useSubmission('Failed to update members', { keepPendingOnSuccess: true });

    useEffect(() => {
        let active = true;
        Promise.all([listUsers(), listGroupMembers(group.id)])
            .then(([allUsers, members]) => {
                if (!active) return;
                setUsers(allUsers);
                setSelected(new Set((members || []).map((member) => member.user_id)));
                setLoadError('');
            })
            .catch((failure) => active && setLoadError(errorMessage(failure, 'Failed to load members')))
            .finally(() => active && setLoading(false));
        return () => { active = false; };
    }, [group.id]);

    const toggle = (id) => setSelected((current) => {
        const next = new Set(current);
        if (next.has(id)) next.delete(id); else next.add(id);
        return next;
    });

    const needle = query.trim().toLowerCase();
    const filtered = users.filter((user) => {
        if (selectedOnly && !selected.has(user.id)) return false;
        if (!needle) return true;
        return [user.username, user.display_name, user.email].filter(Boolean).join(' ').toLowerCase().includes(needle);
    });

    const selectAllMatching = () => setSelected((current) => new Set([...current, ...filtered.map((user) => user.id)]));

    const submit = async (event) => {
        event.preventDefault();
        if (pending) return;
        await run(async () => {
            await setGroupMembers(group.id, [...selected]);
            onSaved();
        });
    };

    return (
        <Modal
            isOpen
            onClose={pending ? undefined : onCancel}
            title="Group members"
            subtitle={group.name}
            variant="drawer"
            size="lg"
            closeOnBackdrop={!pending}
            closeOnEscape={!pending}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={pending}>Cancel</Button>
                    <Button type="submit" form="admin-members-form" variant="primary" isLoading={pending}>Save members</Button>
                </>
            }
        >
            <form id="admin-members-form" onSubmit={submit} className="flex flex-col gap-4">
                {loading ? (
                    <div className="flex justify-center p-6"><Spinner /></div>
                ) : loadError ? (
                    <AdminAlert>{loadError}</AdminAlert>
                ) : users.length === 0 ? (
                    <EmptyState title="No users" message="Create users first." />
                ) : (
                    <div className="flex flex-col gap-3">
                        <div className="flex flex-wrap items-center gap-3">
                            <div className="min-w-[200px] flex-1">
                                <Input
                                    icon={Search}
                                    placeholder="Search members…"
                                    className="h-9"
                                    value={query}
                                    onChange={(event) => setQuery(event.target.value)}
                                />
                            </div>
                            <Checkbox
                                label={`Selected only (${selected.size})`}
                                className="accent-sky-500"
                                checked={selectedOnly}
                                onChange={(event) => setSelectedOnly(event.target.checked)}
                            />
                            <Button type="button" variant="ghost" size="xs" onClick={selectAllMatching} disabled={filtered.length === 0}>
                                Select all
                            </Button>
                            <Button type="button" variant="ghost" size="xs" onClick={() => setSelected(new Set())} disabled={selected.size === 0}>
                                Clear
                            </Button>
                        </div>
                        {filtered.length === 0 ? (
                            <div className="rounded-xl border border-theme-border px-4 py-8 text-center text-xs text-theme-muted">
                                No matching users
                            </div>
                        ) : (
                            <div className="max-h-[60vh] overflow-y-auto rounded-xl border border-theme-border divide-y divide-theme-border">
                                {filtered.map((user) => (
                                    <label key={user.id} className="flex cursor-pointer items-center gap-3 px-3 py-2 text-sm text-theme-text hover:bg-slate-200/50 dark:hover:bg-slate-800/50">
                                        <input type="checkbox" className="h-4 w-4 accent-sky-500" checked={selected.has(user.id)} onChange={() => toggle(user.id)} />
                                        <span className="font-medium">{user.username}</span>
                                        <span className="text-xs text-theme-muted">{user.display_name}</span>
                                    </label>
                                ))}
                            </div>
                        )}
                    </div>
                )}
                <FormError message={error} />
            </form>
        </Modal>
    );
}
