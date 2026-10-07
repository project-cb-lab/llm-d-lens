import { useCallback, useEffect, useRef, useState } from 'react';
import { usePolling } from '../../hooks/usePolling';
import { KeyRound, Plus, ShieldCheck, Trash2, Users } from 'lucide-react';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { Button } from '../ui/Button';
import { Modal } from '../ui/Modal';
import { FormError } from '../ui/FormError';
import { Checkbox, Input, Label, Select } from '../ui/FormControls';
import { Badge, StatusChip } from '../ui/Badge';
import { AsyncState } from '../shared/AsyncState';
import { EmptyState } from '../ui/EmptyState';
import { useNotice } from '../../hooks/useNotice';
import { useSubmission } from '../../hooks/useSubmission';
import { errorMessage } from '../../utils/errorMessage';
import { PermissionGate } from '../../features/auth/PermissionGate';
import { useAuth } from '../../features/auth/useAuth';
import {
    addUserRoleBinding,
    createUser,
    deleteUser,
    listClusters,
    listRoles,
    listUsers,
    resetUserPassword,
} from '../../features/auth/adminClient';
import { AdminClearButton, AdminPanel, AdminSearch, AdminToolbar } from './AdminPanel';
import { AdminAlert } from './AdminAlert';
import { ConfirmDeleteModal } from './ConfirmDeleteModal';
import { RoleBindingsDrawer } from './RoleBindingsDrawer';

function roleScopeTitle(role) {
    if (role.scopeType === 'cluster') return `Cluster: ${role.scopeClusterId || '—'}`;
    if (role.scopeType === 'resource') return `Resource: ${role.scopeResourceType || '—'}:${role.scopeResourceId || '—'}`;
    return 'All clusters';
}

export default function UsersPage({ onToggleMobileNav }) {
    const { principal } = useAuth();
    const [users, setUsers] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [notice, setNotice] = useNotice();
    const [query, setQuery] = useState('');
    const [status, setStatus] = useState('');
    const [dialog, setDialog] = useState(null);
    const [pendingDelete, setPendingDelete] = useState(null);
    const [pendingReset, setPendingReset] = useState(null);
    const [pendingRoles, setPendingRoles] = useState(null);

    const load = useCallback(async () => {
        try {
            setUsers(await listUsers());
            setError('');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to load users'));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);
    usePolling(() => load());

    const needle = query.trim().toLowerCase();
    const filtered = users.filter((user) => {
        if (status && user.status !== status) return false;
        if (!needle) return true;
        return [user.username, user.display_name, user.email].filter(Boolean).join(' ').toLowerCase().includes(needle);
    });

    const confirmDelete = async () => {
        await deleteUser(pendingDelete.id);
        setPendingDelete(null);
        await load();
        setNotice(`Deleted ${pendingDelete.username}`);
    };

    return (
        <ModulePage>
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={Users}
                    title="Users"
                    description="Manage Lens accounts, their status, and passwords."
                    onToggleMobileNav={onToggleMobileNav}
                    actions={
                        <>
                            <PermissionGate permission="user:user:create">
                                <Button variant="sky" size="sm" onClick={() => setDialog({ mode: 'add' })}>
                                    <Plus size={14} /> Add user
                                </Button>
                            </PermissionGate>
                        </>
                    }
                />

                <AdminAlert tone="success">{notice}</AdminAlert>
                {error && users.length > 0 && <AdminAlert>{error}</AdminAlert>}

                <AsyncState
                    loading={loading}
                    error={users.length === 0 ? error : null}
                    empty={users.length === 0}
                    onRetry={load}
                    emptyContent={
                        <EmptyState
                            icon={<Users size={22} />}
                            title="No users yet"
                            message="Create a local account, or connect an external identity provider for directory users."
                            action={<PermissionGate permission="user:user:create"><Button variant="sky" size="sm" onClick={() => setDialog({ mode: 'add' })}><Plus size={14} /> Add user</Button></PermissionGate>}
                        />
                    }
                >
                    <AdminPanel
                        icon={Users}
                        title="Users"
                        count={filtered.length}
                        toolbar={
                            <AdminToolbar>
                                <AdminSearch value={query} onChange={setQuery} placeholder="Search username, name, or email..." />
                                <div className="w-40 shrink-0">
                                    <Select value={status} onChange={(event) => setStatus(event.target.value)} aria-label="Status filter">
                                        <option value="">All statuses</option>
                                        <option value="active">Active</option>
                                        <option value="disabled">Disabled</option>
                                        <option value="locked">Locked</option>
                                    </Select>
                                </div>
                                <AdminClearButton show={Boolean(query || status)} onClick={() => { setQuery(''); setStatus(''); }} />
                            </AdminToolbar>
                        }
                    >
                        {filtered.length === 0 ? (
                            <EmptyState title="No matching users" message="Adjust the filters to see more." />
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full min-w-[44rem] text-left text-sm">
                                    <thead className="bg-slate-950/60 text-xs uppercase tracking-wide text-slate-500">
                                        <tr>
                                            <th className="px-4 py-3">Username</th>
                                            <th className="px-4 py-3">Display name</th>
                                            <th className="px-4 py-3">Email</th>
                                            <th className="px-4 py-3">Source</th>
                                            <th className="px-4 py-3">Groups</th>
                                            <th className="px-4 py-3">Roles</th>
                                            <th className="px-4 py-3">Status</th>
                                            <th className="px-4 py-3 text-right">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {filtered.map((user) => (
                                            <tr key={user.id} className="border-t border-slate-800">
                                                <td className="px-4 py-3 font-medium text-slate-100">{user.username}</td>
                                                <td className="px-4 py-3 text-slate-300">{user.display_name || '—'}</td>
                                                <td className="px-4 py-3 text-slate-400">{user.email || '—'}</td>
                                                <td className="px-4 py-3">
                                                    <span className="rounded-full border border-slate-700/60 bg-slate-900/60 px-2 py-0.5 text-[11px] uppercase tracking-wide text-slate-300">
                                                        {user.auth_source}
                                                    </span>
                                                </td>
                                                <td className="px-4 py-3">
                                                    {(user.groups || []).length === 0 ? (
                                                        <span className="text-slate-600">—</span>
                                                    ) : (
                                                        <div className="flex flex-wrap gap-1">
                                                            {user.groups.map((group) => (
                                                                <Badge key={group.id} tone="info" title={group.source}>
                                                                    {group.name}
                                                                </Badge>
                                                            ))}
                                                        </div>
                                                    )}
                                                </td>
                                                <td className="px-4 py-3">
                                                    {(user.roles || []).length === 0 && (user.inheritedRoles || []).length === 0 ? (
                                                        <span className="text-slate-600">—</span>
                                                    ) : (
                                                        <div className="flex flex-wrap gap-1">
                                                            {(user.roles || []).map((role, index) => (
                                                                <Badge
                                                                    key={`role-${role.name}-${index}`}
                                                                    tone="violet"
                                                                    title={`Direct · ${roleScopeTitle(role)}`}
                                                                >
                                                                    {role.name}
                                                                </Badge>
                                                            ))}
                                                            {(user.inheritedRoles || []).map((role, index) => (
                                                                <Badge
                                                                    key={`inherited-${role.groupId}-${role.name}-${index}`}
                                                                    tone="info"
                                                                    title={`Via group ${role.groupName} · ${roleScopeTitle(role)}`}
                                                                >
                                                                    {role.name}
                                                                </Badge>
                                                            ))}
                                                        </div>
                                                    )}
                                                </td>
                                                <td className="px-4 py-3"><StatusChip status={user.status} /></td>
                                                <td className="px-4 py-3">
                                                    <div className="flex items-center justify-end gap-1">
                                                        {!user.protected && (
                                                            <PermissionGate permission="role:binding:manage">
                                                                <Button variant="ghost" size="icon" onClick={() => setPendingRoles(user)} aria-label={`Roles for ${user.username}`} title="Roles">
                                                                    <ShieldCheck size={14} className="text-sky-400" />
                                                                </Button>
                                                            </PermissionGate>
                                                        )}
                                                        {user.auth_source === 'local' && (
                                                            <PermissionGate permission="user:user:reset-password">
                                                                <Button variant="ghost" size="icon" onClick={() => setPendingReset(user)} aria-label={`Reset password for ${user.username}`} title="Reset password">
                                                                    <KeyRound size={14} />
                                                                </Button>
                                                            </PermissionGate>
                                                        )}
                                                        {!user.protected && user.id !== principal?.userId && (
                                                            <PermissionGate permission="user:user:delete">
                                                                <Button variant="ghost" size="icon" onClick={() => setPendingDelete(user)} aria-label={`Delete ${user.username}`} title="Delete">
                                                                    <Trash2 size={14} className="text-rose-400" />
                                                                </Button>
                                                            </PermissionGate>
                                                        )}
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
                <UserFormModal onCancel={() => setDialog(null)} onSaved={() => { setDialog(null); load(); setNotice('User created'); }} />
            )}
            {pendingRoles && (
                <RoleBindingsDrawer
                    subjectType="user"
                    subjectId={pendingRoles.id}
                    subjectLabel={pendingRoles.username}
                    onClose={() => setPendingRoles(null)}
                    onChange={load}
                />
            )}
            {pendingReset && (
                <ResetPasswordModal
                    user={pendingReset}
                    onCancel={() => setPendingReset(null)}
                    onSaved={() => { setPendingReset(null); setNotice(`Password reset for ${pendingReset.username}`); }}
                />
            )}
            {pendingDelete && (
                <ConfirmDeleteModal
                    title="Delete user"
                    subtitle={pendingDelete.username}
                    warning="The user's sessions are revoked immediately. Resources they own remain and are taken over by cluster maintainers."
                    confirmLabel="Delete user"
                    onCancel={() => setPendingDelete(null)}
                    onDelete={confirmDelete}
                />
            )}
        </ModulePage>
    );
}

function UserFormModal({ onCancel, onSaved }) {
    const { can } = useAuth();
    const mayBind = can('role:binding:manage');
    const [form, setForm] = useState({ username: '', displayName: '', email: '', password: '', confirmPassword: '' });
    const [roles, setRoles] = useState([]);
    const [clusters, setClusters] = useState([]);
    const [selectedRoles, setSelectedRoles] = useState(new Set());
    const [scopeType, setScopeType] = useState('global');
    const [clusterId, setClusterId] = useState('');
    const { pending, error, setError, run } = useSubmission('Failed to create user', { keepPendingOnSuccess: true });
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
    const setField = (field) => (event) => {
        setForm((prev) => ({ ...prev, [field]: event.target.value }));
        if (field === 'password' || field === 'confirmPassword') setError('');
    };
    const toggleRole = (roleId) => setSelectedRoles((current) => {
        const next = new Set(current);
        if (next.has(roleId)) next.delete(roleId); else next.add(roleId);
        return next;
    });

    const submit = async (event) => {
        event.preventDefault();
        if (pending) return;
        if (form.password !== form.confirmPassword) {
            setError('The two passwords do not match.');
            return;
        }
        if (mayBind && selectedRoles.size > 0 && scopeType === 'cluster' && !clusterId) {
            setError('Select a cluster for the chosen scope.');
            return;
        }
        await run(async () => {
            const user = await createUser({
                username: form.username.trim(),
                displayName: form.displayName.trim() || form.username.trim(),
                email: form.email.trim() || null,
                password: form.password,
            });
            if (mayBind && selectedRoles.size > 0) {
                for (const roleId of selectedRoles) {
                    await addUserRoleBinding(user.id, {
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
            title="Add user"
            subtitle="Create a local Lens account. The user can change this password after signing in."
            variant="drawer"
            size="lg"
            closeOnBackdrop={!pending}
            closeOnEscape={!pending}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={pending}>Cancel</Button>
                    <Button type="submit" form="admin-user-form" variant="primary" isLoading={pending}>Add user</Button>
                </>
            }
        >
            <form id="admin-user-form" onSubmit={submit} className="flex flex-col gap-4">
                <div>
                    <Label htmlFor="admin-user-username">Username</Label>
                    <Input ref={nameRef} id="admin-user-username" value={form.username} onChange={setField('username')} placeholder="e.g. alice" required maxLength={150} />
                </div>
                <div>
                    <Label htmlFor="admin-user-display">Display name</Label>
                    <Input id="admin-user-display" value={form.displayName} onChange={setField('displayName')} placeholder="Optional" maxLength={200} />
                </div>
                <div>
                    <Label htmlFor="admin-user-email">Email</Label>
                    <Input id="admin-user-email" type="email" value={form.email} onChange={setField('email')} placeholder="Optional" />
                </div>
                <div>
                    <Label htmlFor="admin-user-password">Password</Label>
                    <Input id="admin-user-password" type="password" autoComplete="new-password" value={form.password} onChange={setField('password')} required />
                </div>
                <div>
                    <Label htmlFor="admin-user-confirm-password">Confirm password</Label>
                    <Input
                        id="admin-user-confirm-password"
                        type="password"
                        autoComplete="new-password"
                        value={form.confirmPassword}
                        onChange={setField('confirmPassword')}
                        required
                    />
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
                                    <Label htmlFor="admin-user-scope">Scope</Label>
                                    <Select id="admin-user-scope" value={scopeType} onChange={(event) => setScopeType(event.target.value)}>
                                        <option value="global">All clusters (global)</option>
                                        <option value="cluster">Specific cluster</option>
                                    </Select>
                                </div>
                                {scopeType === 'cluster' && (
                                    <div>
                                        <Label htmlFor="admin-user-cluster">Cluster</Label>
                                        <Select id="admin-user-cluster" value={clusterId} onChange={(event) => setClusterId(event.target.value)}>
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
                            Optional — you can also assign roles later from the user's Roles drawer.
                        </p>
                    </div>
                )}
                <FormError message={error} />
            </form>
        </Modal>
    );
}

function ResetPasswordModal({ user, onCancel, onSaved }) {
    const [password, setPassword] = useState('');
    const { pending, error, run } = useSubmission('Failed to reset password', { keepPendingOnSuccess: true });
    const inputRef = useRef(null);
    useEffect(() => inputRef.current?.focus(), []);

    const submit = async (event) => {
        event.preventDefault();
        if (pending) return;
        await run(async () => {
            await resetUserPassword(user.id, password);
            onSaved();
        });
    };

    return (
        <Modal
            isOpen
            onClose={pending ? undefined : onCancel}
            title="Reset password"
            subtitle={`${user.username} must set a new password at their next sign-in.`}
            closeOnBackdrop={!pending}
            closeOnEscape={!pending}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={pending}>Cancel</Button>
                    <Button type="submit" form="admin-reset-form" variant="primary" isLoading={pending}>Reset password</Button>
                </>
            }
        >
            <form id="admin-reset-form" onSubmit={submit} className="flex flex-col gap-4">
                <div>
                    <Label htmlFor="admin-reset-password">New password</Label>
                    <Input ref={inputRef} id="admin-reset-password" type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} required />
                </div>
                <FormError message={error} />
            </form>
        </Modal>
    );
}
