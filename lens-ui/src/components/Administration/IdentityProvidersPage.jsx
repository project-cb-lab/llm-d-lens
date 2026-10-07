import { useCallback, useEffect, useRef, useState } from 'react';
import { usePolling } from '../../hooks/usePolling';
import { CheckCircle2, KeyRound, Pencil, Plus, RefreshCw, Trash2, XCircle } from 'lucide-react';
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
import {
    createIdentityProvider,
    deleteIdentityProvider,
    listIdentityProviders,
    syncIdentityProvider,
    testIdentityProvider,
    updateIdentityProvider,
} from '../../features/auth/adminClient';
import { AdminPanel } from './AdminPanel';
import { AdminAlert } from './AdminAlert';
import { ConfirmDeleteModal } from './ConfirmDeleteModal';

const EMPTY = {
    type: 'ldap',
    name: '',
    server_url: '',
    user_base_dn: '',
    group_base_dn: '',
    user_filter: '(uid={username})',
    username_attribute: 'uid',
    display_name_attribute: 'cn',
    email_attribute: 'mail',
    bind_dn: '',
    bind_password: '',
    enabled: true,
    is_default: false,
};

export default function IdentityProvidersPage({ onToggleMobileNav }) {
    const [providers, setProviders] = useState([]);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useNotice();
    const [dialog, setDialog] = useState(null);
    const [pendingDelete, setPendingDelete] = useState(null);
    const [testingId, setTestingId] = useState(null);
    const [syncingId, setSyncingId] = useState(null);
    const [testResults, setTestResults] = useState({});
    const loadedOnce = useRef(false);

    const load = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet && loadedOnce.current) setRefreshing(true);
        try {
            setProviders(await listIdentityProviders());
            setError('');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to load identity providers'));
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

    const runTest = async (provider) => {
        setTestingId(provider.id);
        setTestResults((prev) => ({ ...prev, [provider.id]: null }));
        try {
            const result = await testIdentityProvider(provider.id);
            setTestResults((prev) => ({ ...prev, [provider.id]: result }));
        } catch (failure) {
            setTestResults((prev) => ({ ...prev, [provider.id]: { ok: false, detail: errorMessage(failure, 'Test failed') } }));
        } finally {
            setTestingId(null);
        }
    };

    const runSync = async (provider) => {
        setSyncingId(provider.id);
        try {
            const result = await syncIdentityProvider(provider.id);
            setNotice(`Synced ${result.groups} group(s), ${result.members} member(s) from ${provider.name}`);
            await load({ quiet: true });
        } catch (failure) {
            setError(errorMessage(failure, 'Sync failed'));
        } finally {
            setSyncingId(null);
        }
    };

    const confirmDelete = async () => {
        await deleteIdentityProvider(pendingDelete.id);
        setPendingDelete(null);
        await load();
        setNotice(`Deleted ${pendingDelete.name}`);
    };

    return (
        <ModulePage>
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={KeyRound}
                    title="Identity providers"
                    badge="Experimental"
                    description="Connect external directories (LDAP) so directory users can sign in to Lens. This integration is experimental; behavior and configuration may change."
                    onToggleMobileNav={onToggleMobileNav}
                    actions={
                        <>
                            <Button variant="secondary" size="sm" onClick={load} isLoading={refreshing} disabled={loading}>
                                <RefreshCw size={14} /> Refresh
                            </Button>
                            <PermissionGate permission="idp:provider:configure">
                                <Button variant="sky" size="sm" onClick={() => setDialog({ mode: 'add' })}>
                                    <Plus size={14} /> Add provider
                                </Button>
                            </PermissionGate>
                        </>
                    }
                />

                <AdminAlert tone="success">{notice}</AdminAlert>
                {error && providers.length > 0 && <AdminAlert>{error}</AdminAlert>}

                <AsyncState
                    loading={loading}
                    error={providers.length === 0 ? error : null}
                    empty={providers.length === 0}
                    onRetry={load}
                    emptyContent={
                        <EmptyState
                            icon={<KeyRound size={22} />}
                            title="No external identity providers"
                            message="Lens manages local accounts by default. Add an LDAP provider to authenticate directory users."
                            action={<PermissionGate permission="idp:provider:configure"><Button variant="sky" size="sm" onClick={() => setDialog({ mode: 'add' })}><Plus size={14} /> Add provider</Button></PermissionGate>}
                        />
                    }
                >
                    <AdminPanel icon={KeyRound} title="Providers" count={providers.length}>
                        <div className="overflow-x-auto">
                            <table className="w-full min-w-[46rem] text-left text-sm">
                                <thead className="bg-slate-950/60 text-xs uppercase tracking-wide text-slate-500">
                                    <tr>
                                        <th className="px-4 py-3">Name</th>
                                        <th className="px-4 py-3">Type</th>
                                        <th className="px-4 py-3">Enabled</th>
                                        <th className="px-4 py-3">Default</th>
                                        <th className="px-4 py-3">Test</th>
                                        <th className="px-4 py-3 text-right">Actions</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {providers.map((provider) => {
                                        const result = testResults[provider.id];
                                        const isTesting = testingId === provider.id;
                                        return (
                                            <tr key={provider.id} className="border-t border-slate-800">
                                                <td className="px-4 py-3 font-medium text-slate-100">{provider.name}</td>
                                                <td className="px-4 py-3"><Badge tone="info">{provider.type}</Badge></td>
                                                <td className="px-4 py-3 text-slate-300">{provider.enabled ? 'Yes' : 'No'}</td>
                                                <td className="px-4 py-3 text-slate-300">{provider.is_default ? 'Yes' : '—'}</td>
                                                <td className="px-4 py-3">
                                                    <div className="flex items-center gap-2">
                                                        <Button variant="secondary" size="xs" onClick={() => runTest(provider)} isLoading={isTesting}>Test</Button>
                                                        {result && !isTesting && (
                                                            <span className={`flex items-center gap-1 text-xs ${result.ok ? 'text-emerald-300' : 'text-rose-300'}`} title={result.detail}>
                                                                {result.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                                                            </span>
                                                        )}
                                                    </div>
                                                </td>
                                                <td className="px-4 py-3">
                                                    <div className="flex items-center justify-end gap-1">
                                                        <PermissionGate permission="idp:sync:execute">
                                                            <Button variant="secondary" size="xs" onClick={() => runSync(provider)} isLoading={syncingId === provider.id}>
                                                                Sync
                                                            </Button>
                                                        </PermissionGate>
                                                        <PermissionGate permission="idp:provider:configure">
                                                            <Button variant="ghost" size="icon" onClick={() => setDialog({ mode: 'edit', provider })} aria-label={`Edit ${provider.name}`}>
                                                                <Pencil size={14} />
                                                            </Button>
                                                            <Button variant="ghost" size="icon" onClick={() => setPendingDelete(provider)} aria-label={`Delete ${provider.name}`}>
                                                                <Trash2 size={14} className="text-rose-400" />
                                                            </Button>
                                                        </PermissionGate>
                                                    </div>
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                    </AdminPanel>
                </AsyncState>
            </div>

            {dialog && (
                <ProviderFormModal
                    provider={dialog.mode === 'edit' ? dialog.provider : null}
                    onCancel={() => setDialog(null)}
                    onSaved={(saved) => {
                        setDialog(null);
                        load();
                        setNotice(dialog.mode === 'edit' ? 'Provider updated' : 'Provider created');
                        // Import the directory immediately when it is ready, so
                        // configured users/groups show up without a manual step.
                        if (saved?.enabled && saved?.config?.group_base_dn) {
                            syncIdentityProvider(saved.id).then(() => load({ quiet: true })).catch(() => {});
                        }
                    }}
                />
            )}
            {pendingDelete && (
                <ConfirmDeleteModal
                    title="Delete identity provider"
                    subtitle={pendingDelete.name}
                    warning="Directory users keep their Lens accounts but can no longer sign in through this provider."
                    confirmLabel="Delete provider"
                    onCancel={() => setPendingDelete(null)}
                    onDelete={confirmDelete}
                />
            )}
        </ModulePage>
    );
}

function ProviderFormModal({ provider, onCancel, onSaved }) {
    const isEdit = Boolean(provider);
    const [form, setForm] = useState(() => (provider
        ? {
            type: provider.type || 'ldap',
            name: provider.name || '',
            server_url: provider.config?.server_url || '',
            user_base_dn: provider.config?.user_base_dn || '',
            group_base_dn: provider.config?.group_base_dn || '',
            user_filter: provider.config?.user_filter || '(uid={username})',
            username_attribute: provider.config?.username_attribute || 'uid',
            display_name_attribute: provider.config?.display_name_attribute || 'cn',
            email_attribute: provider.config?.email_attribute || 'mail',
            bind_dn: provider.config?.bind_dn || '',
            bind_password: '',
            enabled: Boolean(provider.enabled),
            is_default: Boolean(provider.is_default),
        }
        : EMPTY));
    const { pending, error, run } = useSubmission(`Failed to ${isEdit ? 'update' : 'create'} provider`, { keepPendingOnSuccess: true });
    const nameRef = useRef(null);
    useEffect(() => nameRef.current?.focus(), []);
    const setField = (field) => (event) => setForm((prev) => ({ ...prev, [field]: event.target.value }));

    const submit = async (event) => {
        event.preventDefault();
        if (pending) return;
        await run(async () => {
            const payload = {
                type: form.type,
                name: form.name.trim(),
                enabled: form.enabled,
                isDefault: form.is_default,
                config: {
                    server_url: form.server_url.trim(),
                    user_base_dn: form.user_base_dn.trim(),
                    group_base_dn: form.group_base_dn.trim(),
                    user_filter: form.user_filter.trim(),
                    username_attribute: form.username_attribute.trim(),
                    display_name_attribute: form.display_name_attribute.trim(),
                    email_attribute: form.email_attribute.trim(),
                    bind_dn: form.bind_dn.trim(),
                },
                syncMode: 'login',
            };
            if (form.bind_password) payload.secret = form.bind_password;
            const saved = isEdit ? await updateIdentityProvider(provider.id, payload) : await createIdentityProvider(payload);
            onSaved(saved);
        });
    };

    return (
        <Modal
            isOpen
            onClose={pending ? undefined : onCancel}
            title={isEdit ? 'Edit identity provider' : 'Add identity provider'}
            subtitle="LDAP directory. The bind password is encrypted at rest and never returned."
            variant="drawer"
            size="lg"
            closeOnBackdrop={!pending}
            closeOnEscape={!pending}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={pending}>Cancel</Button>
                    <Button type="submit" form="admin-idp-form" variant="primary" isLoading={pending}>{isEdit ? 'Save changes' : 'Add provider'}</Button>
                </>
            }
        >
            <form id="admin-idp-form" onSubmit={submit} className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div className="sm:col-span-2">
                    <Label htmlFor="admin-idp-name">Name</Label>
                    <Input ref={nameRef} id="admin-idp-name" value={form.name} onChange={setField('name')} placeholder="e.g. corp-ldap" required maxLength={150} />
                </div>
                <div className="sm:col-span-2">
                    <Label htmlFor="admin-idp-url">Server URL</Label>
                    <Input id="admin-idp-url" value={form.server_url} onChange={setField('server_url')} placeholder="ldaps://ldap.example.com:636" required />
                </div>
                <div>
                    <Label htmlFor="admin-idp-base">User base DN</Label>
                    <Input id="admin-idp-base" value={form.user_base_dn} onChange={setField('user_base_dn')} placeholder="ou=people,dc=example,dc=com" required />
                </div>
                <div>
                    <Label htmlFor="admin-idp-filter">User filter</Label>
                    <Input id="admin-idp-filter" value={form.user_filter} onChange={setField('user_filter')} placeholder="(uid={username})" />
                </div>
                <div>
                    <Label htmlFor="admin-idp-username-attr">Username attribute</Label>
                    <Input id="admin-idp-username-attr" value={form.username_attribute} onChange={setField('username_attribute')} />
                </div>
                <div>
                    <Label htmlFor="admin-idp-display-attr">Display name attribute</Label>
                    <Input id="admin-idp-display-attr" value={form.display_name_attribute} onChange={setField('display_name_attribute')} />
                </div>
                <div>
                    <Label htmlFor="admin-idp-email-attr">Email attribute</Label>
                    <Input id="admin-idp-email-attr" value={form.email_attribute} onChange={setField('email_attribute')} />
                </div>
                <div className="sm:col-span-2">
                    <Label htmlFor="admin-idp-group-base">Group base DN</Label>
                    <Input id="admin-idp-group-base" value={form.group_base_dn} onChange={setField('group_base_dn')} placeholder="ou=groups,dc=example,dc=com" />
                    <p className="mt-1.5 text-[11px] text-slate-500">
                        Optional. Groups under this base are imported on Sync and on sign-in so they can be granted roles and shared to.
                    </p>
                </div>
                <div>
                    <Label htmlFor="admin-idp-bind-dn">Bind DN</Label>
                    <Input id="admin-idp-bind-dn" value={form.bind_dn} onChange={setField('bind_dn')} placeholder="cn=svc,dc=example,dc=com" />
                </div>
                <div className="sm:col-span-2">
                    <Label htmlFor="admin-idp-bind-pw">Bind password {isEdit && <span className="font-normal text-slate-500">(leave blank to keep)</span>}</Label>
                    <Input id="admin-idp-bind-pw" type="password" autoComplete="new-password" value={form.bind_password} onChange={setField('bind_password')} />
                </div>
                <div className="flex items-center gap-6 sm:col-span-2">
                    <Checkbox label="Enabled" checked={form.enabled} onChange={(e) => setForm((prev) => ({ ...prev, enabled: e.target.checked }))} />
                    <Checkbox label="Default provider" checked={form.is_default} onChange={(e) => setForm((prev) => ({ ...prev, is_default: e.target.checked }))} />
                </div>
                <div className="sm:col-span-2"><FormError message={error} /></div>
            </form>
        </Modal>
    );
}
