import { useCallback, useEffect, useRef, useState } from 'react';
import { KeyRound, Plus, RefreshCw, ShieldAlert, Trash2 } from 'lucide-react';
import { usePolling } from '../../hooks/usePolling';
import { useClipboard } from '../../hooks/useClipboard';
import { useSubmission } from '../../hooks/useSubmission';
import { AsyncState } from '../shared/AsyncState';
import { Button } from '../ui/Button';
import { Badge } from '../ui/Badge';
import { EmptyState } from '../ui/EmptyState';
import { Modal } from '../ui/Modal';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { SectionLabel } from '../ui/SectionLabel';
import { cn } from '../../utils/cn';
import { PermissionGate } from '../../features/auth/PermissionGate';
import {
    createModelToken,
    listModelTokens,
    regenerateModelToken,
    revokeModelToken,
} from './modelServiceBackend';
import { CARD, CELL, CELL_MUTED, TABLE_HEAD, TABLE_ROW, maskedKeyHint } from './modelServiceStyles';

// User-facing "API keys" page: self-manage model access tokens (`lens-mk-...`).
// The plaintext token is shown exactly once, right after create/reset.
export function ApiKeysPage({ onToggleMobileNav }) {
    const [tokens, setTokens] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [createOpen, setCreateOpen] = useState(false);
    const [tokenName, setTokenName] = useState('default');
    const [revealed, setRevealed] = useState(null); // { token, id }
    const [pendingRevoke, setPendingRevoke] = useState(null);
    const loadedOnceRef = useRef(false);
    const { copied, copy } = useClipboard();

    const load = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet && loadedOnceRef.current) setLoading(true);
        try {
            const tokenItems = await listModelTokens();
            setTokens(tokenItems);
            setError('');
        } catch (failure) {
            setError(failure?.message || 'Failed to load API keys');
        } finally {
            loadedOnceRef.current = true;
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);
    usePolling(() => load({ quiet: true }));

    const createSubmission = useSubmission('Failed to create token');
    const regenerateSubmission = useSubmission('Failed to reset token');
    const revokeSubmission = useSubmission('Failed to revoke token');

    const handleCreate = () => {
        createSubmission.run(async () => {
            const created = await createModelToken(tokenName.trim() || 'default');
            setCreateOpen(false);
            setTokenName('default');
            setRevealed({ token: created.token, id: created.id });
            await load({ quiet: true });
        });
    };

    const handleRegenerate = () => {
        regenerateSubmission.run(async () => {
            const created = await regenerateModelToken('default');
            setRevealed({ token: created.token, id: created.id });
            await load({ quiet: true });
        });
    };

    const handleRevoke = () => {
        revokeSubmission.run(async () => {
            await revokeModelToken(pendingRevoke.id);
            setPendingRevoke(null);
            await load({ quiet: true });
        });
    };

    return (
        <ModulePage>
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={KeyRound}
                    title="API keys"
                    description="Personal tokens (lens-mk-...) used to call published models through the model gateway."
                    onToggleMobileNav={onToggleMobileNav}
                />

                {error && (
                    <p role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                        {error}
                    </p>
                )}

                <PermissionGate permission="model-service:token:manage">
                    <section className={cn(CARD, 'p-6')}>
                        <div className="flex flex-wrap items-center justify-between gap-2">
                            <SectionLabel>{`API keys (${tokens.length})`}</SectionLabel>
                            <div className="flex items-center gap-2">
                                <Button variant="secondary" size="sm" onClick={handleRegenerate} isLoading={regenerateSubmission.pending} disabled={tokens.length === 0}>
                                    <RefreshCw size={14} /> Reset
                                </Button>
                                <Button variant="sky" size="sm" onClick={() => setCreateOpen(true)}>
                                    <Plus size={14} /> New key
                                </Button>
                            </div>
                        </div>
                        <div className="mt-4">
                            {(createSubmission.error || regenerateSubmission.error || revokeSubmission.error) && (
                                <p role="alert" className="mb-3 text-xs text-rose-300">
                                    {createSubmission.error || regenerateSubmission.error || revokeSubmission.error}
                                </p>
                            )}
                            <AsyncState loading={loading} error={null} empty={tokens.length === 0}
                                emptyContent={<EmptyState icon={<KeyRound size={22} />} title="No API keys"
                                    message="Create a key to call model services with the OpenAI SDK or curl." />}>
                                <div className="overflow-x-auto">
                                    <table className="w-full min-w-[44rem] text-left text-sm">
                                        <thead>
                                            <tr>
                                                <th className={TABLE_HEAD}>Name</th>
                                                <th className={TABLE_HEAD}>Key</th>
                                                <th className={TABLE_HEAD}>Status</th>
                                                <th className={TABLE_HEAD}>Last used</th>
                                                <th className={cn(TABLE_HEAD, 'text-right')}>Actions</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {tokens.map((token) => (
                                                <tr key={token.id} className={TABLE_ROW}>
                                                    <td className={cn(CELL, 'text-slate-100')}>{token.name}</td>
                                                    <td className={cn(CELL, 'font-mono text-xs text-slate-400')}>{maskedKeyHint(token.tokenHint)}</td>
                                                    <td className={CELL}>
                                                        {token.status === 'active'
                                                            ? <Badge tone="success">active</Badge>
                                                            : <Badge tone="neutral">revoked</Badge>}
                                                    </td>
                                                    <td className={CELL_MUTED}>
                                                        {token.lastUsedAt ? new Date(token.lastUsedAt).toLocaleString() : 'never'}
                                                    </td>
                                                    <td className={cn(CELL, 'text-right')}>
                                                        {token.status === 'active' && (
                                                            <Button variant="ghost" size="icon" aria-label={`Revoke ${token.name}`}
                                                                onClick={() => setPendingRevoke(token)}>
                                                                <Trash2 size={14} className="text-rose-400" />
                                                            </Button>
                                                        )}
                                                    </td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                            </AsyncState>
                        </div>
                    </section>
                </PermissionGate>
            </div>

            <Modal
                isOpen={createOpen}
                onClose={() => setCreateOpen(false)}
                title="Create API key"
                footer={
                    <>
                        <Button variant="secondary" size="sm" onClick={() => setCreateOpen(false)}>Cancel</Button>
                        <Button variant="sky" size="sm" onClick={handleCreate} isLoading={createSubmission.pending}>Create</Button>
                    </>
                }
            >
                <label className="block text-xs text-slate-400" htmlFor="key-name">Key name</label>
                <input
                    id="key-name"
                    className="mt-1 w-full rounded-lg border border-slate-800/80 bg-slate-950/50 px-3 py-2 text-sm text-slate-200 outline-none"
                    value={tokenName}
                    maxLength={100}
                    onChange={(event) => setTokenName(event.target.value)}
                />
            </Modal>

            <Modal
                isOpen={Boolean(revealed)}
                onClose={() => setRevealed(null)}
                title="Copy your key now"
                closeOnBackdrop={false}
                footer={<Button variant="sky" size="sm" onClick={() => setRevealed(null)}>I saved it</Button>}
            >
                <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
                    <ShieldAlert size={16} className="mt-0.5 shrink-0" />
                    <span>This is the only time the full key is shown. Store it safely; it cannot be retrieved again.</span>
                </div>
                <div className="mt-3 flex items-center gap-2">
                    <code className="flex-1 truncate rounded-lg border border-slate-800/80 bg-slate-950/50 px-3 py-2 font-mono text-xs text-slate-200">
                        {revealed?.token}
                    </code>
                    <Button variant="secondary" size="sm" onClick={() => copy(revealed?.token || '')}>
                        {copied ? 'Copied' : 'Copy'}
                    </Button>
                </div>
            </Modal>

            <Modal
                isOpen={Boolean(pendingRevoke)}
                onClose={() => setPendingRevoke(null)}
                title="Revoke API key"
                footer={
                    <>
                        <Button variant="secondary" size="sm" onClick={() => setPendingRevoke(null)}>Cancel</Button>
                        <Button variant="danger" size="sm" onClick={handleRevoke} isLoading={revokeSubmission.pending}>Revoke</Button>
                    </>
                }
            >
                <p className="text-sm text-slate-400">
                    Revoke <span className="font-mono text-slate-200">{maskedKeyHint(pendingRevoke?.tokenHint)}</span>?
                    Any client using it will stop working immediately.
                </p>
            </Modal>
        </ModulePage>
    );
}

export default ApiKeysPage;
