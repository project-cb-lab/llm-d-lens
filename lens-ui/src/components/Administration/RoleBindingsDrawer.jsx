import { useCallback, useEffect, useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { FormError } from '../ui/FormError';
import { Label, Select } from '../ui/FormControls';
import { Badge } from '../ui/Badge';
import { Spinner } from '../ui/Spinner';
import { EmptyState } from '../ui/EmptyState';
import { useSubmission } from '../../hooks/useSubmission';
import { errorMessage } from '../../utils/errorMessage';
import {
    addGroupRoleBinding,
    addUserRoleBinding,
    listClusters,
    listGroupRoleBindings,
    listRoles,
    listUserRoleBindings,
    removeGroupRoleBinding,
    removeUserRoleBinding,
} from '../../features/auth/adminClient';
import { AdminAlert } from './AdminAlert';

/**
 * Assign roles to a user or group at a scope (global or a specific cluster).
 * Shared by the Users and Groups pages. `onChange` (optional) fires after
 * each successful add/remove so the caller's own list (e.g. a Roles column)
 * can refresh immediately, without waiting for the drawer to close.
 */
export function RoleBindingsDrawer({ subjectType, subjectId, subjectLabel, onClose, onChange }) {
    const isGroup = subjectType === 'group';
    const [bindings, setBindings] = useState([]);
    const [roles, setRoles] = useState([]);
    const [clusters, setClusters] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [roleId, setRoleId] = useState('');
    const [scopeType, setScopeType] = useState('global');
    const [clusterId, setClusterId] = useState('');
    const { pending, error: saveError, setError: setSaveError, run } = useSubmission('Failed to update roles');

    const loadBindings = useCallback(
        () => (isGroup ? listGroupRoleBindings(subjectId) : listUserRoleBindings(subjectId)),
        [isGroup, subjectId]
    );

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const [bindingList, roleList, clusterResponse] = await Promise.all([loadBindings(), listRoles(), listClusters()]);
            setBindings(bindingList || []);
            setRoles(roleList || []);
            setClusters(clusterResponse?.items || []);
            setError('');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to load roles'));
        } finally {
            setLoading(false);
        }
    }, [loadBindings]);

    useEffect(() => {
        load();
    }, [load]);

    const roleName = (id) => roles.find((role) => role.id === id)?.name || id;
    const clusterName = (id) => clusters.find((cluster) => cluster.id === id)?.name || id;

    const add = async (event) => {
        event.preventDefault();
        if (pending || !roleId) return;
        if (scopeType === 'cluster' && !clusterId) return;
        setSaveError('');
        await run(async () => {
            const payload = {
                roleId,
                scopeType,
                scopeClusterId: scopeType === 'cluster' ? clusterId : null,
            };
            if (isGroup) await addGroupRoleBinding(subjectId, payload);
            else await addUserRoleBinding(subjectId, payload);
            setRoleId('');
            setScopeType('global');
            setClusterId('');
            await load();
            onChange?.();
        });
    };

    const remove = async (binding) => {
        setSaveError('');
        await run(async () => {
            if (isGroup) await removeGroupRoleBinding(subjectId, binding.id);
            else await removeUserRoleBinding(subjectId, binding.id);
            await load();
            onChange?.();
        });
    };

    return (
        <Modal
            isOpen
            onClose={pending ? undefined : onClose}
            title="Roles"
            subtitle={subjectLabel}
            variant="drawer"
            size="md"
            closeOnBackdrop={!pending}
            closeOnEscape={!pending}
            footer={<Button variant="secondary" onClick={onClose} disabled={pending}>Close</Button>}
        >
            <div className="flex flex-col gap-4">
                <form onSubmit={add} className="flex flex-col gap-3 rounded-xl border border-theme-border p-3">
                    <div>
                        <Label htmlFor="binding-role">Role</Label>
                        <Select id="binding-role" value={roleId} onChange={(event) => setRoleId(event.target.value)} required>
                            <option value="">Select a role…</option>
                            {roles.map((role) => (
                                <option key={role.id} value={role.id}>{role.name}</option>
                            ))}
                        </Select>
                    </div>
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <div>
                            <Label htmlFor="binding-scope">Scope</Label>
                            <Select id="binding-scope" value={scopeType} onChange={(event) => setScopeType(event.target.value)}>
                                <option value="global">All clusters (global)</option>
                                <option value="cluster">Specific cluster</option>
                            </Select>
                        </div>
                        {scopeType === 'cluster' && (
                            <div>
                                <Label htmlFor="binding-cluster">Cluster</Label>
                                <Select id="binding-cluster" value={clusterId} onChange={(event) => setClusterId(event.target.value)} required>
                                    <option value="">Select a cluster…</option>
                                    {clusters.map((cluster) => (
                                        <option key={cluster.id} value={cluster.id}>{cluster.name || cluster.id}</option>
                                    ))}
                                </Select>
                            </div>
                        )}
                    </div>
                    <div>
                        <Button type="submit" variant="primary" size="sm" isLoading={pending} disabled={!roleId || (scopeType === 'cluster' && !clusterId)}>
                            <Plus size={14} /> Add role
                        </Button>
                    </div>
                </form>

                <FormError message={saveError} />
                {error && <AdminAlert>{error}</AdminAlert>}

                {loading ? (
                    <div className="flex justify-center p-4"><Spinner /></div>
                ) : bindings.length === 0 ? (
                    <EmptyState title="No roles assigned" message="Assign a role above to grant permissions." />
                ) : (
                    <ul className="flex flex-col gap-2">
                        {bindings.map((binding) => (
                            <li key={binding.id} className="flex items-center justify-between gap-3 rounded-lg border border-theme-border px-3 py-2 text-sm">
                                <span className="flex items-center gap-2">
                                    <Badge tone="violet">{roleName(binding.role_id)}</Badge>
                                    <span className="text-xs text-theme-muted">
                                        {binding.scope_type === 'global'
                                            ? 'All clusters'
                                            : binding.scope_type === 'cluster'
                                                ? `Cluster: ${clusterName(binding.scope_cluster_id)}`
                                                : `Resource: ${binding.scope_resource_type}:${binding.scope_resource_id}`}
                                    </span>
                                </span>
                                <Button variant="ghost" size="icon" onClick={() => remove(binding)} disabled={pending} aria-label="Remove role">
                                    <Trash2 size={14} className="text-rose-400" />
                                </Button>
                            </li>
                        ))}
                    </ul>
                )}
            </div>
        </Modal>
    );
}

export default RoleBindingsDrawer;
