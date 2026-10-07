import { useCallback, useEffect, useMemo, useState } from 'react';
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
    getExecutionShareOptions,
    getRunShareOptions,
    grantExecutionShare,
    grantRunShare,
    listExecutionShares,
    listRunShares,
    revokeExecutionShare,
    revokeRunShare,
} from '../OptimizationWorkspace/remoteDeployBackend';
import { AdminAlert } from '../Administration/AdminAlert';

const DEFAULT_ROLE = 'end-user';

// Explain what the chosen role actually confers here, so it is clear the role
// is scoped to this one deployment and is not the person's account-level role.
const ROLE_ACCESS_HINTS = {
    'end-user': 'Can view, connect, and operate this deployment. Cannot delete or re-share it.',
    maintainer: 'Full control of this deployment, including deleting and re-sharing it.',
    admin: 'Full control of this deployment, including deleting and re-sharing it.',
};

/**
 * Grant or revoke resource-scoped access to one deployment (design section
 * 7.7). Supports both an execution target and a run target; the server derives
 * the cluster and enforces who may share.
 */
export function DeploymentShareDrawer({ targetType = 'execution', targetId, targetLabel, onClose }) {
    const isRun = targetType === 'run';
    const [shares, setShares] = useState([]);
    const [users, setUsers] = useState([]);
    const [groups, setGroups] = useState([]);
    const [roles, setRoles] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [subjectType, setSubjectType] = useState('user');
    const [subjectId, setSubjectId] = useState('');
    const [roleId, setRoleId] = useState('');
    const { pending, error: saveError, setError: setSaveError, run } = useSubmission('Failed to update access');

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const [shareList, options] = await Promise.all([
                isRun ? listRunShares(targetId) : listExecutionShares(targetId),
                isRun ? getRunShareOptions(targetId) : getExecutionShareOptions(targetId),
            ]);
            setShares(shareList || []);
            setUsers(options?.users || []);
            setGroups(options?.groups || []);
            setRoles(options?.roles || []);
            setError('');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to load access'));
        } finally {
            setLoading(false);
        }
    }, [isRun, targetId]);

    useEffect(() => {
        load();
    }, [load]);

    useEffect(() => {
        if (!roleId && roles.length) {
            setRoleId((roles.find((role) => role.name === DEFAULT_ROLE) || roles[0]).id);
        }
    }, [roles, roleId]);

    const subjectLabel = useMemo(() => {
        const byId = new Map([
            ...users.map((user) => [user.id, user.username || user.displayName || user.id]),
            ...groups.map((group) => [group.id, group.name || group.id]),
        ]);
        return (share) => byId.get(share.subjectId) || share.subjectId;
    }, [users, groups]);

    const roleName = (id) => roles.find((role) => role.id === id)?.name || id;
    const selectedRoleName = roles.find((role) => role.id === roleId)?.name || '';
    const roleHint = ROLE_ACCESS_HINTS[selectedRoleName]
        || 'Grants this role’s permissions on this deployment only.';

    const grant = async (event) => {
        event.preventDefault();
        if (pending || !subjectId || !roleId) return;
        setSaveError('');
        await run(async () => {
            const payload = { subjectType, subjectId, roleId };
            if (isRun) await grantRunShare(targetId, payload);
            else await grantExecutionShare(targetId, payload);
            setSubjectId('');
            await load();
        });
    };

    const revoke = async (share) => {
        setSaveError('');
        await run(async () => {
            if (isRun) await revokeRunShare(targetId, share.id);
            else await revokeExecutionShare(targetId, share.id);
            await load();
        });
    };

    return (
        <Modal
            isOpen
            onClose={pending ? undefined : onClose}
            title="Share deployment"
            subtitle={targetLabel}
            variant="drawer"
            size="md"
            closeOnBackdrop={!pending}
            closeOnEscape={!pending}
            footer={<Button variant="secondary" onClick={onClose} disabled={pending}>Close</Button>}
        >
            <div className="flex flex-col gap-4">
                <p className="rounded-xl border border-theme-border px-3 py-2.5 text-[11px] leading-5 text-theme-muted">
                    Sharing adds access to <span className="font-semibold text-theme-text">this deployment only</span>.
                    The person&apos;s account roles do not change — the role you pick below is granted just for this deployment.
                </p>
                <form onSubmit={grant} className="flex flex-col gap-3 rounded-xl border border-theme-border p-3">
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <div>
                            <Label htmlFor="share-subject-type">Share with</Label>
                            <Select
                                id="share-subject-type"
                                value={subjectType}
                                onChange={(event) => { setSubjectType(event.target.value); setSubjectId(''); }}
                            >
                                <option value="user">User</option>
                                <option value="group">Group</option>
                            </Select>
                        </div>
                        <div>
                            <Label htmlFor="share-subject">{subjectType === 'group' ? 'Group' : 'User'}</Label>
                            <Select id="share-subject" value={subjectId} onChange={(event) => setSubjectId(event.target.value)} required>
                                <option value="">Select a {subjectType}…</option>
                                {(subjectType === 'group' ? groups : users).map((subject) => (
                                    <option key={subject.id} value={subject.id}>
                                        {subject.name || subject.username || subject.id}
                                    </option>
                                ))}
                            </Select>
                        </div>
                    </div>
                    <div>
                        <Label htmlFor="share-role">Permission on this deployment</Label>
                        <Select id="share-role" value={roleId} onChange={(event) => setRoleId(event.target.value)} required>
                            <option value="">Select a role…</option>
                            {roles.map((role) => (
                                <option key={role.id} value={role.id}>{role.name}</option>
                            ))}
                        </Select>
                        <p className="mt-1 text-[10px] leading-4 text-theme-muted">{roleHint}</p>
                    </div>
                    <div>
                        <Button type="submit" variant="primary" size="sm" isLoading={pending} disabled={!subjectId || !roleId}>
                            <Plus size={14} /> Share
                        </Button>
                    </div>
                </form>

                <FormError message={saveError} />
                {error && <AdminAlert>{error}</AdminAlert>}

                {loading ? (
                    <div className="flex justify-center p-4"><Spinner /></div>
                ) : shares.length === 0 ? (
                    <EmptyState title="Not shared" message="Share this deployment with a user or group above." />
                ) : (
                    <ul className="flex flex-col gap-2">
                        {shares.map((share) => (
                            <li key={share.id} className="flex items-center justify-between gap-3 rounded-lg border border-theme-border px-3 py-2 text-sm">
                                <span className="flex min-w-0 items-center gap-2">
                                    <Badge tone={share.subjectType === 'group' ? 'warning' : 'violet'}>{subjectLabel(share)}</Badge>
                                    <span className="truncate text-xs text-theme-muted">{roleName(share.roleId)}</span>
                                </span>
                                <Button variant="ghost" size="icon" onClick={() => revoke(share)} disabled={pending} aria-label="Revoke access">
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

export default DeploymentShareDrawer;
