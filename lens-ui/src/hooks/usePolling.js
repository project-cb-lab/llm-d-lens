import { useEffect, useRef } from 'react';

/**
 * Periodically run `callback` to keep page data fresh without a full reload.
 *
 * Behavior shared by every polling page:
 * - the interval is cleared on unmount and rebuilt when the options change;
 * - polling pauses while the tab is hidden and refreshes immediately when it
 *   becomes visible again;
 * - a tick is skipped while the previous async call is still running, so slow
 *   responses never pile up.
 *
 * `immediate` only controls the first tick: pages normally do their own initial
 * load, so the default is to wait one interval before the first refresh.
 */
export function usePolling(callback, { intervalMs = 5000, enabled = true, immediate = false } = {}) {
    const savedCallback = useRef(callback);
    useEffect(() => {
        savedCallback.current = callback;
    }, [callback]);

    useEffect(() => {
        if (!enabled || !Number.isFinite(intervalMs) || intervalMs <= 0) return undefined;
        let timer = null;
        let running = false;
        let cancelled = false;

        const tick = async () => {
            if (running || cancelled) return;
            running = true;
            try {
                await savedCallback.current();
            } finally {
                running = false;
            }
        };

        const start = () => {
            if (timer === null) timer = window.setInterval(tick, intervalMs);
        };
        const stop = () => {
            if (timer !== null) {
                window.clearInterval(timer);
                timer = null;
            }
        };
        const onVisibilityChange = () => {
            if (document.hidden) {
                stop();
                return;
            }
            tick();
            start();
        };

        if (!document.hidden) {
            if (immediate) tick();
            start();
        }
        document.addEventListener('visibilitychange', onVisibilityChange);
        return () => {
            cancelled = true;
            stop();
            document.removeEventListener('visibilitychange', onVisibilityChange);
        };
    }, [enabled, immediate, intervalMs]);
}
