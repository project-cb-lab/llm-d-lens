import { useCallback, useEffect, useRef, useState } from 'react';
import { usePolling } from '../../hooks/usePolling';
import { Pencil, Plus, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { Button } from '../ui/Button';
import { Modal } from '../ui/Modal';
import { FormError } from '../ui/FormError';
import { Checkbox, Input, Label } from '../ui/FormControls';
import { Badge } from '../ui/Badge';
import { AsyncState } from '../shared/AsyncState';
import { EmptyState } from '../ui/EmptyState';
import { useNotice } from '../../hooks/useNotice';
import { useSubmission } from '../../hooks/useSubmission';
import { errorMessage } from '../../utils/errorMessage';
import { PermissionGate } from '../../features/auth/PermissionGate';
import { createRole, deleteRole, listPermissions, listRoles, setRolePermissions } from '../../features/auth/adminClient';
import { AdminClearButton, AdminPanel, AdminSearch, AdminToolbar } from './AdminPanel';
import { AdminAlert } from './AdminAlert';
import { ConfirmDeleteModal } from './ConfirmDeleteModal';

export default function RolesPage({ onToggleMobileNav }) {
    const [roles, setRoles] = useState([]);
    const [catalog, setCatalog] = useState([]);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useNotice();
    const [query, setQuery] = useState('');
    const [dialog, setDialog] = useState(null);
    const [pendingDelete, setPendingDelete] = useState(null);
    const loadedOnce = useRef(false);

    const load = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet && loadedOnce.current) setRefreshing(true);
        try {
            const [roleList, permissions] = await Promise.all([listRoles(), listPermissions()]);
            setRoles(roleList);
            setCatalog(permissions);
            setError('');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to load roles'));
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
    const filtered = roles.filter((role) => !needle || role.name.toLowerCase().includes(needle) || (role.description || '').toLowerCase().includes(needle));

    const confirmDelete = async () => {
        await deleteRole(pendingDelete.id);
        setPendingDelete(null);
        await load();
        setNotice(`Deleted ${pendingDelete.name}`);
    };

    return (
        <ModulePage>
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={ShieldCheck}
                    title="Roles"
                    description="Roles bundle permissions. Assign them to users or groups at a scope."
                    onToggleMobileNav={onToggleMobileNav}
                    actions={
                        <>
                            <Button variant="secondary" size="sm" onClick={load} isLoading={refreshing} disabled={loading}>
                                <RefreshCw size={14} /> Refresh
                            </Button>
                            <PermissionGate permission="role:role:create">
                                <Button variant="sky" size="sm" onClick={() => setDialog({ mode: 'add' })}>
                                    <Plus size={14} /> Add role
                                </Button>
                            </PermissionGate>
                        </>
                    }
                />

                <AdminAlert tone="success">{notice}</AdminAlert>
                {error && roles.length > 0 && <AdminAlert>{error}</AdminAlert>}

                <AsyncState
                    loading={loading}
                    error={roles.length === 0 ? error : null}
                    empty={roles.length === 0}
                    onRetry={load}
                    emptyContent={<EmptyState icon={<ShieldCheck size={22} />} title="No roles" message="Built-in roles are seeded automatically." />}
                >
                    <AdminPanel
                        icon={ShieldCheck}
                        title="Roles"
                        count={filtered.length}
                        toolbar={
                            <AdminToolbar>
                                <AdminSearch value={query} onChange={setQuery} placeholder="Search role name or description..." />
                                <AdminClearButton show={Boolean(query)} onClick={() => setQuery('')} />
                            </AdminToolbar>
                        }
                    >
                        {filtered.length === 0 ? (
                            <EmptyState title="No matching roles" message="Adjust the search to see more." />
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full min-w-[40rem] text-left text-sm">
                                    <thead className="bg-slate-950/60 text-xs uppercase tracking-wide text-slate-500">
                                        <tr>
                                            <th className="px-4 py-3">Name</th>
                                            <th className="px-4 py-3">Description</th>
                                            <th className="px-4 py-3">Type</th>
                                            <th className="px-4 py-3">Permissions</th>
                                            <th className="px-4 py-3 text-right">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {filtered.map((role) => (
                                            <tr key={role.id} className="border-t border-slate-800">
                                                <td className="px-4 py-3 font-medium text-slate-100">{role.name}</td>
                                                <td className="px-4 py-3 text-slate-400">{role.description || '—'}</td>
                                                <td className="px-4 py-3">
                                                    <Badge tone={role.is_builtin ? 'info' : 'neutral'}>{role.is_builtin ? 'built-in' : 'custom'}</Badge>
                                                </td>
                                                <td className="px-4 py-3 text-slate-300">{role.permissions.length}</td>
                                                <td className="px-4 py-3">
                                                    <div className="flex items-center justify-end gap-1">
                                                        <PermissionGate permission="role:role:update">
                                                            <Button variant="ghost" size="icon" disabled={role.is_builtin} onClick={() => setDialog({ mode: 'edit', role })} aria-label={`Edit ${role.name}`}>
                                                                <Pencil size={14} />
                                                            </Button>
                                                        </PermissionGate>
                                                        <PermissionGate permission="role:role:delete">
                                                            <Button variant="ghost" size="icon" disabled={role.is_builtin} onClick={() => setPendingDelete(role)} aria-label={`Delete ${role.name}`}>
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

            {dialog && (
                <RoleFormModal
                    role={dialog.mode === 'edit' ? dialog.role : null}
                    catalog={catalog}
                    onCancel={() => setDialog(null)}
                    onSaved={() => { setDialog(null); load(); setNotice(dialog.mode === 'edit' ? 'Role updated' : 'Role created'); }}
                />
            )}
            {pendingDelete && (
                <ConfirmDeleteModal
                    title="Delete role"
                    subtitle={pendingDelete.name}
                    warning="Bindings using this role are removed. Users and groups keep their other roles."
                    confirmLabel="Delete role"
                    onCancel={() => setPendingDelete(null)}
                    onDelete={confirmDelete}
                />
            )}
        </ModulePage>
    );
}

const DOMAIN_LABELS = {
    user: 'Users', group: 'Groups', role: 'Roles', idp: 'Identity providers',
    session: 'Sessions', audit: 'Audit', system: 'System', cluster: 'Clusters',
    storage: 'Storage', 'model-cache': 'Model cache', deployment: 'Deployments',
    evaluate: 'Evaluation', configuration: 'Configuration', candidate: 'Candidates',
    guide: 'Guide planning', simulation: 'Simulation', 'ai-provider': 'External providers',
    monitoring: 'Observability', playground: 'Assistant', 'remote-deploy': 'Remote deploy',
    'deploy-poc': 'Deploy PoC', config: 'Runtime config', mcp: 'MCP',
};

function groupByDomain(catalog) {
    const groups = new Map();
    for (const permission of catalog) {
        const list = groups.get(permission.domain) || [];
        list.push(permission);
        groups.set(permission.domain, list);
    }
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
}

function RoleFormModal({ role, catalog, onCancel, onSaved }) {
    const isEdit = Boolean(role);
    const [name, setName] = useState(role?.name || '');
    const [description, setDescription] = useState(role?.description || '');
    const [selected, setSelected] = useState(() => new Set(role?.permissions || []));
    const { pending, error, run } = useSubmission(`Failed to ${isEdit ? 'update' : 'create'} role`, { keepPendingOnSuccess: true });
    const nameRef = useRef(null);
    useEffect(() => nameRef.current?.focus(), []);

    const toggle = (code) => setSelected((current) => {
        const next = new Set(current);
        if (next.has(code)) next.delete(code); else next.add(code);
        return next;
    });

    const submit = async (event) => {
        event.preventDefault();
        if (pending) return;
        await run(async () => {
            if (isEdit) {
                await setRolePermissions(role.id, [...selected]);
            } else {
                await createRole({ name: name.trim(), description: description.trim(), permissions: [...selected] });
            }
            onSaved();
        });
    };

    return (
        <Modal
            isOpen
            onClose={pending ? undefined : onCancel}
            title={isEdit ? `Edit permissions: ${role.name}` : 'Add role'}
            subtitle="Select the permissions this role grants. Scope is chosen when you assign the role."
            variant="drawer"
            size="xl"
            closeOnBackdrop={!pending}
            closeOnEscape={!pending}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={pending}>Cancel</Button>
                    <Button type="submit" form="admin-role-form" variant="primary" isLoading={pending}>
                        {isEdit ? 'Save permissions' : 'Add role'}
                    </Button>
                </>
            }
        >
            <form id="admin-role-form" onSubmit={submit} className="flex flex-col gap-4">
                {!isEdit && (
                    <>
                        <div>
                            <Label htmlFor="admin-role-name">Name</Label>
                            <Input ref={nameRef} id="admin-role-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. reviewer" required maxLength={100} />
                        </div>
                        <div>
                            <Label htmlFor="admin-role-desc">Description</Label>
                            <Input id="admin-role-desc" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Optional" />
                        </div>
                    </>
                )}
                <div className="rounded-xl border border-theme-border p-3 flex flex-col gap-4">
                    {groupByDomain(catalog).map(([domain, permissions]) => (
                        <div key={domain}>
                            <p className="mb-1.5 text-[10px] font-bold uppercase tracking-widest text-slate-500">
                                {DOMAIN_LABELS[domain] || domain}
                            </p>
                            <div className="grid grid-cols-1 gap-1 sm:grid-cols-2 lg:grid-cols-3">
                                {permissions.map((permission) => (
                                    <Checkbox
                                        key={permission.code}
                                        checked={selected.has(permission.code)}
                                        onChange={() => toggle(permission.code)}
                                        label={
                                            <span className="flex flex-col">
                                                <span className="font-mono text-[11px] text-theme-text">{permission.code}</span>
                                                <span className="text-[10px] text-theme-muted">{permission.description}</span>
                                            </span>
                                        }
                                    />
                                ))}
                            </div>
                        </div>
                    ))}
                </div>
                <FormError message={error} />
            </form>
        </Modal>
    );
}
