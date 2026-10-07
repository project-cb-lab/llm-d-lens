/** Display API errors without discarding domain fallback text. */
export function errorMessage(error, fallback = 'Request failed') {
    if (!error) return fallback;
    if (typeof error.details === 'string' && error.details) return error.details;
    return error.message || fallback;
}
