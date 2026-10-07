/**
 * Hugging Face environment variables are admin-managed.
 *
 * The platform injects Hugging Face endpoints, tokens, cache locations and
 * offline switches from the cluster's saved proxy, model cache and token
 * configuration. A user override must not be able to replace them, so the
 * deployment editor and the planning API reject these names.
 */
export const ADMIN_MANAGED_ENVIRONMENT_PREFIXES = ['HF_', 'HUGGING', 'TRANSFORMERS_'];

export function isAdminManagedEnvironmentVariable(name) {
    const normalized = String(name || '').trim().toUpperCase();
    return Boolean(normalized) && ADMIN_MANAGED_ENVIRONMENT_PREFIXES.some((prefix) => normalized.startsWith(prefix));
}

export function adminManagedEnvironmentMessage(name) {
    return `${name} is managed by the platform administrator and cannot be set here.`;
}
