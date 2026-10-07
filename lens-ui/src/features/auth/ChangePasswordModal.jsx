import { useEffect, useRef, useState } from 'react';
import { Modal } from '../../components/ui/Modal';
import { Button } from '../../components/ui/Button';
import { Input, Label } from '../../components/ui/FormControls';
import { FormError } from '../../components/ui/FormError';
import { useSubmission } from '../../hooks/useSubmission';
import { changePassword } from './authClient';

/** Change the signed-in user's own password (voluntary, from the account menu). */
export function ChangePasswordModal({ onClose, onSaved }) {
    const [form, setForm] = useState({ oldPassword: '', newPassword: '', confirmPassword: '' });
    const { pending, error, setError, run } = useSubmission('Failed to change password');
    const firstRef = useRef(null);
    useEffect(() => firstRef.current?.focus(), []);

    const setField = (field) => (event) => {
        setForm((prev) => ({ ...prev, [field]: event.target.value }));
        setError('');
    };

    const submit = async (event) => {
        event.preventDefault();
        if (pending) return;
        if (form.newPassword !== form.confirmPassword) {
            setError('The two new passwords do not match.');
            return;
        }
        await run(async () => {
            await changePassword(form.oldPassword, form.newPassword);
            onSaved?.();
        });
    };

    return (
        <Modal
            isOpen
            onClose={pending ? undefined : onClose}
            title="Change password"
            subtitle="Update your own account password."
            closeOnBackdrop={!pending}
            closeOnEscape={!pending}
            footer={
                <>
                    <Button variant="secondary" onClick={onClose} disabled={pending}>Cancel</Button>
                    <Button type="submit" form="account-change-password-form" variant="primary" isLoading={pending}>
                        Update password
                    </Button>
                </>
            }
        >
            <form id="account-change-password-form" onSubmit={submit} className="flex flex-col gap-4">
                <div>
                    <Label htmlFor="account-old-password">Current password</Label>
                    <Input ref={firstRef} id="account-old-password" type="password" autoComplete="current-password" value={form.oldPassword} onChange={setField('oldPassword')} required />
                </div>
                <div>
                    <Label htmlFor="account-new-password">New password</Label>
                    <Input id="account-new-password" type="password" autoComplete="new-password" value={form.newPassword} onChange={setField('newPassword')} required />
                    <p className="mt-1 text-[11px] text-slate-500">
                        At least 12 characters, mixing 3 of: lowercase, uppercase, digit, symbol.
                    </p>
                </div>
                <div>
                    <Label htmlFor="account-confirm-password">Confirm new password</Label>
                    <Input id="account-confirm-password" type="password" autoComplete="new-password" value={form.confirmPassword} onChange={setField('confirmPassword')} required />
                </div>
                <FormError message={error} />
            </form>
        </Modal>
    );
}

export default ChangePasswordModal;
