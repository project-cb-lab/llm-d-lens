import { FormError } from '../ui/FormError';
import { useSubmission } from '../../hooks/useSubmission';
import { useEffect, useRef, useState } from 'react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Input, Label, Textarea } from '../ui/FormControls';
import { deploymentTitle } from './deploymentPresentation';

const NAME_LIMIT = 120;
const DESCRIPTION_LIMIT = 1000;

export function EditDeploymentModal({ deployment, onCancel, onSave }) {
    const [displayName, setDisplayName] = useState(deployment?.display_name || '');
    const [description, setDescription] = useState(deployment?.description || '');
    const { pending: saving, error, run } = useSubmission('Failed to update deployment', { keepPendingOnSuccess: true });
    const nameRef = useRef(null);

    useEffect(() => {
        nameRef.current?.focus();
    }, []);

    if (!deployment) return null;

    const nameTooLong = displayName.trim().length > NAME_LIMIT;
    const descriptionTooLong = description.trim().length > DESCRIPTION_LIMIT;
    const unchanged =
        displayName.trim() === (deployment.display_name || '') && description.trim() === (deployment.description || '');
    const invalid = nameTooLong || descriptionTooLong || unchanged;

    const submit = async (event) => {
        event.preventDefault();
        if (invalid || saving) return;
        await run(() => onSave({ displayName: displayName.trim(), description: description.trim() }));
    };

    return (
        <Modal
            isOpen
            onClose={saving ? undefined : onCancel}
            title="Edit deployment"
            subtitle={deploymentTitle(deployment)}
            closeOnBackdrop={!saving}
            closeOnEscape={!saving}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={saving}>Cancel</Button>
                    <Button variant="sky" type="submit" form="edit-deployment-form" isLoading={saving} disabled={invalid}>
                        {saving ? 'Saving' : 'Save changes'}
                    </Button>
                </>
            }
        >
            <form id="edit-deployment-form" onSubmit={submit} className="flex flex-col gap-4">
                <div>
                    <Label htmlFor="deployment-display-name">Display name</Label>
                    <Input
                        id="deployment-display-name"
                        ref={nameRef}
                        value={displayName}
                        onChange={(event) => setDisplayName(event.target.value)}
                        placeholder={deployment.name || deployment.model || 'Deployment name'}
                        maxLength={NAME_LIMIT}
                        error={nameTooLong}
                        disabled={saving}
                    />
                    <div className="mt-1 text-[10px] text-slate-500">{displayName.trim().length}/{NAME_LIMIT}</div>
                </div>

                <div>
                    <Label htmlFor="deployment-description">Description</Label>
                    <Textarea
                        id="deployment-description"
                        value={description}
                        onChange={(event) => setDescription(event.target.value)}
                        placeholder="What is this deployment for?"
                        maxLength={DESCRIPTION_LIMIT}
                        rows={5}
                        error={descriptionTooLong}
                        disabled={saving}
                    />
                    <div className="mt-1 text-[10px] text-slate-500">{description.trim().length}/{DESCRIPTION_LIMIT}</div>
                </div>

                <p className="rounded-lg border border-slate-800/60 bg-slate-900/40 px-3 py-2 text-[11px] text-slate-500">
                    Runtime configuration is immutable. Model, namespace, image, topology, endpoint, and cluster cannot be edited here.
                </p>

                <FormError message={error} />
            </form>
        </Modal>
    );
}

export default EditDeploymentModal;
