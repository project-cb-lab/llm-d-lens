import React, { useState } from 'react';
import { AlertTriangle, KeyRound } from 'lucide-react';
import { Button, Input } from '../../components/ui';
import { FormError } from '../../components/ui/FormError';
import { errorMessage } from '../../utils/errorMessage';
import { changePassword } from './authClient';
import { useAuth } from './useAuth';

export default function ChangePasswordPage() {
    const { refresh, logout } = useAuth();
    const [oldPassword, setOldPassword] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [error, setError] = useState('');
    const [submitting, setSubmitting] = useState(false);

    const handleSubmit = async (event) => {
        event.preventDefault();
        setError('');
        setSubmitting(true);
        try {
            await changePassword(oldPassword, newPassword);
            await refresh();
        } catch (changeError) {
            setError(errorMessage(changeError));
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <div className="flex min-h-screen w-full items-center justify-center bg-slate-950 p-6 font-sans text-slate-100 antialiased">
            <div className="w-full max-w-sm">
                <div className="mb-8 flex items-center justify-center gap-3">
                    <a href="https://llm-d.ai" target="_blank" rel="noopener noreferrer" aria-label="llm-d">
                        <img src="https://llm-d.ai/img/llm-d-logotype-and-icon.png" alt="llm-d" className="h-9 w-auto object-contain" />
                    </a>
                    <span className="select-none text-3xl leading-none text-slate-500" aria-hidden="true">·</span>
                    <div className="flex items-center gap-2.5">
                        <img src="/lens_logo.png" alt="Lens" className="h-9 w-9 object-contain" />
                        <span className="select-none bg-gradient-to-r from-sky-500 via-cyan-400 to-teal-400 bg-clip-text text-3xl font-bold tracking-wide text-transparent">
                            Lens
                        </span>
                    </div>
                </div>

                <div className="mb-5 flex gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2.5">
                    <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-300" aria-hidden="true" />
                    <p className="text-xs leading-relaxed text-amber-100">
                        For your security, set a new password before using Lens.
                    </p>
                </div>

                <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
                    <Input
                        icon={KeyRound}
                        type="password"
                        autoComplete="current-password"
                        autoFocus
                        placeholder="Current password"
                        className="h-11"
                        value={oldPassword}
                        onChange={(event) => setOldPassword(event.target.value)}
                        required
                    />
                    <div>
                        <Input
                            icon={KeyRound}
                            type="password"
                            autoComplete="new-password"
                            placeholder="New password"
                            className="h-11"
                            value={newPassword}
                            onChange={(event) => setNewPassword(event.target.value)}
                            required
                        />
                        <p className="mt-1 text-[11px] text-slate-500">
                            At least 12 characters, mixing 3 of: lowercase, uppercase, digit, symbol.
                        </p>
                    </div>
                    <FormError message={error} />
                    <div className="mt-2 flex gap-2">
                        <Button type="submit" variant="primary" size="md" isLoading={submitting} className="h-11 flex-1">
                            Update password
                        </Button>
                        <Button type="button" variant="secondary" size="md" onClick={logout} disabled={submitting} className="h-11">
                            Sign out
                        </Button>
                    </div>
                </form>
            </div>
        </div>
    );
}
