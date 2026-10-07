import { FormError } from '../ui/FormError';
import { useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Select } from '../ui/FormControls';
import { getModelCacheEntryLogs } from './modelCacheBackend';
import { errorMessage, sourceDisplayName, statusDotClass, statusLabel } from './modelCachePresentation';

const LOG_POLL_INTERVAL_MS = 3000;

export function ModelCacheLogsModal({ entry, onClose }) {
    const nodes = entry?.nodeProgress || [];
    const [node, setNode] = useState(nodes[0]?.node || '');
    const [logs, setLogs] = useState('');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const loadedOnceRef = useRef(false);

    const load = async (targetNode) => {
        if (!entry) return;
        // Only show the full-page "Loading…" state the first time; later
        // polls refresh the text in place so it doesn't flicker/scroll.
        if (!loadedOnceRef.current) setLoading(true);
        setError('');
        try {
            const result = await getModelCacheEntryLogs(entry.id, { node: targetNode || undefined });
            setLogs(result);
            loadedOnceRef.current = true;
        } catch (failure) {
            setError(errorMessage(failure, 'Failed to load logs'));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        loadedOnceRef.current = false;
        load(node);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [entry?.id, node]);

    // Keep the log view live while the modal is open -- download logs change
    // continuously while a model is still downloading, so a manual refresh
    // button alone isn't enough.
    useEffect(() => {
        if (!entry) return undefined;
        const timer = setInterval(() => load(node), LOG_POLL_INTERVAL_MS);
        return () => clearInterval(timer);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [entry?.id, node]);

    if (!entry) return null;

    return (
        <Modal
            isOpen
            onClose={onClose}
            title="Download logs"
            subtitle={sourceDisplayName(entry)}
            size="lg"
            footer={<Button variant="secondary" onClick={onClose}>Close</Button>}
        >
            <div className="flex flex-col gap-3">
                {nodes.length > 1 && (
                    <div className="flex items-center gap-2">
                        <Select value={node} onChange={(event) => setNode(event.target.value)} className="w-56">
                            {nodes.map((progress) => (
                                <option key={progress.node} value={progress.node}>{progress.node}</option>
                            ))}
                        </Select>
                        <Button variant="secondary" size="icon" onClick={() => load(node)} disabled={loading} title="Refresh logs">
                            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} aria-hidden="true" />
                        </Button>
                    </div>
                )}

                {nodes.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                        {nodes.map((progress) => (
                            <span
                                key={progress.node}
                                className="inline-flex items-center gap-1.5 rounded-full border border-slate-700 px-2 py-0.5 text-[11px] text-slate-300"
                                title={progress.failureDetail || statusLabel(progress.status)}
                            >
                                <span className={`h-1.5 w-1.5 rounded-full ${statusDotClass(progress.status)}`} aria-hidden="true" />
                                {progress.node === '*' ? 'shared' : progress.node}: {statusLabel(progress.status)}
                            </span>
                        ))}
                    </div>
                )}

                <FormError message={error} />

                <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 font-mono text-[11px] leading-relaxed text-slate-300">
                    {loading ? 'Loading logs…' : (logs || 'No logs available yet.')}
                </pre>
            </div>
        </Modal>
    );
}

export default ModelCacheLogsModal;
