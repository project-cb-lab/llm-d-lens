import { useContext } from 'react';
import { AuthContext } from './AuthContext';

/** Access the auth context; must be called inside <AuthProvider>. */
export function useAuth() {
    return useContext(AuthContext);
}
