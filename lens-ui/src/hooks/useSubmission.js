import { useRef, useState } from 'react';
import { createSubmissionGuard } from '../utils/submissionGuard';
import { errorMessage } from '../utils/errorMessage';

export function useSubmission(fallback, { keepPendingOnSuccess = false } = {}) {
    const [pending, setPending] = useState(false);
    const [error, setError] = useState('');
    const guard = useRef(null);
    if (guard.current === null) guard.current = createSubmissionGuard();
    const run = (action) => guard.current(action, {
        onStart: () => { setPending(true); setError(''); },
        onError: (failure) => setError(errorMessage(failure, fallback)),
        onSettled: () => setPending(false),
        keepPendingOnSuccess,
    });
    return { pending, error, setError, run };
}
