import React, { useEffect, useState } from 'react';
import { KeyRound, User } from 'lucide-react';
import { Button, Checkbox, Input } from '../../components/ui';
import { FormError } from '../../components/ui/FormError';
import { errorMessage } from '../../utils/errorMessage';
import { fetchAuthProviders } from './authClient';
import { useAuth } from './useAuth';

export default function LoginPage() {
    const { login } = useAuth();
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const [providers, setProviders] = useState([{ type: 'local', name: 'Lens' }]);
    const [error, setError] = useState('');
    const [remember, setRemember] = useState(false);
    const [submitting, setSubmitting] = useState(false);

    useEffect(() => {
        let active = true;
        fetchAuthProviders()
            .then((items) => {
                if (active && Array.isArray(items) && items.length > 0) setProviders(items);
            })
            .catch(() => {});
        return () => {
            active = false;
        };
    }, []);

    const handleSubmit = async (event) => {
        event.preventDefault();
        setError('');
        setSubmitting(true);
        try {
            await login(username.trim(), password, remember);
        } catch (loginError) {
            setError(errorMessage(loginError));
        } finally {
            setSubmitting(false);
        }
    };

    const externalProviders = providers.filter((provider) => provider.type !== 'local');

    return (
        <div className="flex min-h-screen w-full items-center justify-center bg-slate-950 p-6 font-sans text-slate-100 antialiased">
            <div className="w-full max-w-sm">
                <div className="mb-10 flex items-center justify-center gap-3">
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

                <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
                    <Input
                        id="login-username"
                        icon={User}
                        autoComplete="username"
                        autoFocus
                        placeholder="Username"
                        className="h-11"
                        value={username}
                        onChange={(event) => setUsername(event.target.value)}
                        required
                    />
                    <Input
                        id="login-password"
                        icon={KeyRound}
                        type="password"
                        autoComplete="current-password"
                        placeholder="Password"
                        className="h-11"
                        value={password}
                        onChange={(event) => setPassword(event.target.value)}
                        required
                    />
                    <Checkbox
                        label={<span className="text-slate-100">Keep me logged in</span>}
                        className="accent-sky-500"
                        checked={remember}
                        onChange={(event) => setRemember(event.target.checked)}
                    />
                    <FormError message={error} />
                    <Button type="submit" variant="sky" size="md" isLoading={submitting} className="mt-2 h-11 w-full">
                        Sign in
                    </Button>
                </form>

                {externalProviders.length > 0 && (
                    <p className="mt-4 text-center text-xs text-slate-500">
                        Directory sign-in available for: {externalProviders.map((provider) => provider.name).join(', ')}
                    </p>
                )}
            </div>
        </div>
    );
}
