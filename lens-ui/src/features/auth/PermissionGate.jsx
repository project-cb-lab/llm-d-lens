import React from 'react';
import { useAuth } from './useAuth';

/**
 * Render children only when the principal holds `permission` (a single code,
 * or an array of codes evaluated as any-of).
 * Hiding is a UX affordance; the backend still enforces every action.
 */
export function PermissionGate({ permission, fallback = null, children }) {
    const { can } = useAuth();
    const allowed = Array.isArray(permission) ? permission.some((code) => can(code)) : can(permission);
    return allowed ? children : fallback;
}
