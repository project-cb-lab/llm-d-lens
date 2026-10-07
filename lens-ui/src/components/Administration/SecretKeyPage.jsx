import { useCallback, useEffect, useState } from 'react';
import { KeyRound, RefreshCw, RotateCcw, Trash2 } from 'lucide-react';
import { usePolling } from '../../hooks/usePolling';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { Button } from '../ui/Button';
import { Modal } from '../ui/Modal';
import { Badge } from '../ui/Badge';
import { Input, Label } from '../ui/FormControls';
import { FormError } from '../ui/FormError';
import { Spinner } from '../ui/Spinner';
import { EmptyState } from '../ui/EmptyState';
import { useNotice } from '../../hooks/useNotice';
import { useSubmission } from '../../hooks/useSubmission';
import { errorMessage } from '../../utils/errorMessage';
import { PermissionGate } from '../../features/auth/PermissionGate';
import { clearOldSecretKeys, getSecretKeyStatus, rotateSecretKey } from '../../features/auth/adminClient';
import { AdminPanel } from './AdminPanel';
import { AdminAlert } from './AdminAlert';
import { ConfirmDeleteModal } from './ConfirmDeleteModal';

const SOURCE_LABELS = {
    environment: 'Environment',
    file: 'Stored file',
    unconfigured: 'Not configured',
};

function InfoRow({ label, children }) {
    return (
        <div className="flex items-center justify-between gap-4 border-b border-slate-800/60 py-2.5 last:border-b-0">
            <span className="text-xs text-slate-400">{label}</span>
            <span className="text-xs font-medium text-slate-200">{children}</span>
        </div>
    );
}

function RotateKeyModal({ onCancel, onRotated }) {
    const [newKey, setNewKey] = useState('');
    const { pending, error, run } = useSubmission('Failed to rotate the master key', { keepPendingOnSuccess: true });

    const submit = async (event) => {
        event.preventDefault();
        if (pending) return;
        await run(async () => {
            const result = await rotateSecretKey(newKey.trim());
            onRotated(result);
        });
    };

    return (
        <Modal
            isOpen
            onClose={pending ? undefined : onCancel}
            title="Rotate master key"
            subtitle="Stored provider secrets are re-encrypted with the new key; the previous key is kept for fallback until you clear it."
            closeOnBackdrop={!pending}
            closeOnEscape={!pending}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={pending}>
                        Cancel
                    </Button>
                    <Button type="submit" form="rotate-master-key-form" variant="primary" isLoading={pending}>
                        {pending ? 'Rotating' : 'Rotate key'}
                    </Button>
                </>
            }
        >
            <form id="rotate-master-key-form" onSubmit={submit} className="flex flex-col gap-4">
                <div>
                    <Label htmlFor="rotate-master-key-input">New key (optional)</Label>
                    <Input
                        id="rotate-master-key-input"
                        type="password"
                        autoComplete="new-password"
                        value={newKey}
                        onChange={(event) => setNewKey(event.target.value)}
                        placeholder="Leave blank to generate a strong key"
                    />
                    <p className="mt-1.5 text-[11px] text-slate-500">
                        Use at least 16 characters. The key is never shown again after saving.
                    </p>
                </div>
                <FormError message={error} />
            </form>
        </Modal>
    );
}

export default function SecretKeyPage({ onToggleMobileNav }) {
    const [status, setStatus] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [refreshing, setRefreshing] = useState(false);
    const [notice, setNotice] = useNotice();
    const [rotateOpen, setRotateOpen] = useState(false);
    const [clearOpen, setClearOpen] = useState(false);

    const load = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet) setLoading(true);
        else setRefreshing(true);
        try {
            setStatus(await getSecretKeyStatus());
            setError('');
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to load master key status'));
        } finally {
            setLoading(false);
            setRefreshing(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);
    usePolling(() => load({ quiet: true }));

    const onRotated = (result) => {
        setRotateOpen(false);
        setNotice(`Master key rotated; ${result.rotatedSecrets} provider secret(s) re-encrypted`);
        load();
    };

    const onCleared = async () => {
        await clearOldSecretKeys();
        setClearOpen(false);
        setNotice('Previous master keys cleared');
        load();
    };

    return (
        <ModulePage>
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={KeyRound}
                    title="Master key"
                    description="Encrypts stored external-provider secrets such as LDAP bind passwords. Rotate it here; previous keys are kept only as decryption fallback."
                    onToggleMobileNav={onToggleMobileNav}
                    actions={
                        <Button variant="secondary" size="sm" onClick={() => load()} isLoading={refreshing} disabled={loading}>
                            <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> Refresh
                        </Button>
                    }
                />

                <AdminAlert tone="success">{notice}</AdminAlert>
                <AdminAlert tone="error">{error}</AdminAlert>

                {loading && !status ? (
                    <div className="flex justify-center py-16">
                        <Spinner size="lg" />
                    </div>
                ) : !status ? (
                    <EmptyState
                        icon={KeyRound}
                        title="Master key status unavailable"
                        message="The backend did not return a master-key status."
                    />
                ) : (
                    <AdminPanel icon={KeyRound} title="Stored-secret master key">
                        <div className="flex flex-col px-1">
                            <InfoRow label="Source">
                                <Badge tone={status.envLocked ? 'warning' : 'neutral'}>
                                    {SOURCE_LABELS[status.source] || status.source}
                                </Badge>
                            </InfoRow>
                            <InfoRow label="Key fingerprint">
                                <span className="font-mono">{status.fingerprint || '—'}</span>
                            </InfoRow>
                            <InfoRow label="Key length">{status.keyLength}</InfoRow>
                            <InfoRow label="Provider secrets encrypted">{status.providerSecretCount}</InfoRow>
                            <InfoRow label="Previous keys retained">{status.oldKeyCount}</InfoRow>
                            <InfoRow label="Stored at">
                                <span className="font-mono text-[11px] text-slate-400">{status.path}</span>
                            </InfoRow>
                        </div>

                        {status.envLocked ? (
                            <p className="px-1 pt-4 text-xs text-amber-300">
                                LENS_SECRET_KEY is set through the environment, so the stored key is ignored and cannot be
                                rotated here. Update the environment variable and restart Lens instead.
                            </p>
                        ) : (
                            <PermissionGate
                                permission="system:secret:manage"
                                fallback={
                                    <p className="px-1 pt-4 text-xs text-slate-500">
                                        You have read-only access; rotating the key requires the
                                        <span className="font-mono"> system:secret:manage </span> permission.
                                    </p>
                                }
                            >
                                <div className="flex flex-wrap items-center gap-3 px-1 pt-4">
                                    <Button variant="primary" onClick={() => setRotateOpen(true)}>
                                        <RotateCcw className="mr-1.5 h-3.5 w-3.5" /> Rotate key
                                    </Button>
                                    <Button
                                        variant="dangerOutline"
                                        onClick={() => setClearOpen(true)}
                                        disabled={status.oldKeyCount === 0}
                                    >
                                        <Trash2 className="mr-1.5 h-3.5 w-3.5" /> Clear previous keys
                                    </Button>
                                </div>
                            </PermissionGate>
                        )}
                    </AdminPanel>
                )}
            </div>

            {rotateOpen && <RotateKeyModal onCancel={() => setRotateOpen(false)} onRotated={onRotated} />}
            {clearOpen && (
                <ConfirmDeleteModal
                    title="Clear previous master keys"
                    subtitle="Only current ciphertext uses the active key."
                    warning="Any provider secret still encrypted with a previous key will become unreadable. Re-save those providers before clearing."
                    confirmLabel="Clear keys"
                    onCancel={() => setClearOpen(false)}
                    onDelete={onCleared}
                />
            )}
        </ModulePage>
    );
}
