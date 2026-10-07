import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { AuthContext } from './AuthContext';
import { fetchSession, login as loginRequest, logout as logoutRequest } from './authClient';
import { canAccessView, hasPermission } from './permissions';

/**
 * Holds the current principal and exposes permission helpers. The session is
 * a revocable httpOnly cookie; this provider only mirrors the resolved
 * principal returned by /api/v1/auth/session.
 */
export function AuthProvider({ children }) {
    const [status, setStatus] = useState('loading');
    const [principal, setPrincipal] = useState(null);

    const refresh = useCallback(async () => {
        try {
            const data = await fetchSession();
            setPrincipal(data);
            setStatus('authenticated');
            return data;
        } catch {
            setPrincipal(null);
            setStatus('anonymous');
            return null;
        }
    }, []);

    useEffect(() => {
        refresh();
        const onExpired = () => {
            setPrincipal(null);
            setStatus('anonymous');
        };
        window.addEventListener('prism:auth-expired', onExpired);
        return () => window.removeEventListener('prism:auth-expired', onExpired);
    }, [refresh]);

    const login = useCallback(async (username, password, remember = false) => {
        const data = await loginRequest(username, password, remember);
        setPrincipal(data.principal);
        setStatus('authenticated');
        return data;
    }, []);

    const logout = useCallback(async () => {
        try {
            await logoutRequest();
        } finally {
            setPrincipal(null);
            setStatus('anonymous');
        }
    }, []);

    const value = useMemo(() => {
        const permissions = principal?.permissions ?? [];
        return {
            status,
            principal,
            permissions,
            can: (permission) => hasPermission(permissions, permission),
            canView: (view) => canAccessView(permissions, view),
            login,
            logout,
            refresh,
        };
    }, [status, principal, login, logout, refresh]);

    return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
