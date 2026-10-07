/** One active submission per instance; successful destructive dialogs stay locked. */
export function createSubmissionGuard() {
    let pending = false;
    return async (action, { onStart, onError, onSettled, keepPendingOnSuccess = false } = {}) => {
        if (pending) return;
        pending = true;
        let succeeded = false;
        try {
            onStart?.();
            const result = await action();
            succeeded = true;
            return result;
        } catch (error) {
            if (!onError) throw error;
            onError(error);
        } finally {
            if (!succeeded || !keepPendingOnSuccess) {
                pending = false;
                onSettled?.();
            }
        }
    };
}
