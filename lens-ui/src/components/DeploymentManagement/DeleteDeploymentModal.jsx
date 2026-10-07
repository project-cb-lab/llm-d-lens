import { useSubmission } from '../../hooks/useSubmission';
import { useEffect, useRef, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { DeleteConfirmation } from '../ui/DeleteConfirmation';
import { Checkbox, Input, Label } from '../ui/FormControls';
import {
    deleteConfirmationPhrase,
    deploymentTitle,
    isDeletionInProgressStatus,
} from './deploymentPresentation';

export function DeleteDeploymentModal({ deployment, clusterNameById = {}, onCancel, onDelete }) {
    const [confirmation, setConfirmation] = useState('');
    const [deleteNamespace, setDeleteNamespace] = useState(true);
    const { pending: deleting, error, run } = useSubmission('Failed to delete deployment', { keepPendingOnSuccess: true });
    const inputRef = useRef(null);

    useEffect(() => {
        inputRef.current?.focus();
    }, []);

    if (!deployment) return null;

    const phrase = deleteConfirmationPhrase(deployment);
    const inProgress = isDeletionInProgressStatus(deployment);
    const confirmed = confirmation.trim() === phrase;

    const submit = async (event) => {
        event.preventDefault();
        if (!confirmed || deleting) return;
        await run(() => onDelete({ deleteNamespace }));
    };

    return (
        <DeleteConfirmation
            onCancel={onCancel}
            pending={deleting}
            error={error}
            title="Delete deployment"
            subtitle={deploymentTitle(deployment)}
            formId="delete-deployment-form"
            onSubmit={submit}
            confirmLabel={deleting ? 'Deleting' : 'Delete deployment'}
            confirmDisabled={!confirmed}
        >
            <div className="flex gap-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2.5">
                <AlertTriangle size={16} className="mt-0.5 shrink-0 text-rose-300" aria-hidden="true" />
                <p className="text-xs leading-relaxed text-rose-100">
                    Prism uninstalls deployment monitoring and permanently removes this deployment record.
                    This action cannot be undone.
                </p>
            </div>

            <dl className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
                <div>
                    <dt className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Execution ID</dt>
                    <dd className="mt-0.5 break-all font-mono text-[11px] text-slate-300">{deployment.execution_id}</dd>
                </div>
                <div>
                    <dt className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Cluster</dt>
                    <dd className="mt-0.5 break-all text-slate-300">{clusterNameById[deployment.cluster_id] || deployment.cluster_id || '—'}</dd>
                </div>
                <div className="sm:col-span-2">
                    <dt className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Namespace</dt>
                    <dd className="mt-0.5 break-all font-mono text-[11px] text-slate-300">{deployment.namespace || '—'}</dd>
                </div>
            </dl>

            {inProgress && (
                <p role="alert" className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                    This deployment is still being created or rolled back. Deleting it now cancels the
                    in-flight operation and cleans up any resources it already created.
                </p>
            )}

            <div className="rounded-lg border border-slate-700/70 bg-slate-800/40 px-3 py-2.5">
                <Checkbox
                    id="delete-namespace"
                    checked={deleteNamespace}
                    onChange={(event) => setDeleteNamespace(event.target.checked)}
                    disabled={deleting}
                    label="Also delete the Kubernetes namespace"
                />
                <p className="mt-1.5 pl-6 text-[11px] leading-relaxed text-slate-400">
                    When checked, Prism also deletes namespace{' '}
                    <span className="font-mono text-slate-300">{deployment.namespace || '—'}</span> and all
                    resources in it. Leave unchecked to remove only this record and keep the namespace intact.
                </p>
            </div>

            <div>
                <Label htmlFor="delete-confirmation">
                    Type <span className="font-mono text-slate-300">{phrase}</span> to confirm
                </Label>
                <Input
                    id="delete-confirmation"
                    ref={inputRef}
                    value={confirmation}
                    onChange={(event) => setConfirmation(event.target.value)}
                    autoComplete="off"
                    disabled={deleting}
                />
            </div>
        </DeleteConfirmation>
    );
}

export default DeleteDeploymentModal;
