import { useEffect, useMemo, useRef, useState } from 'react';
import { Loader2, Search } from 'lucide-react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Input } from '../ui/FormControls';
import { errorMessage } from './modelCachePresentation';
import { getHuggingFaceModelDetail, searchHuggingFaceModels } from './modelCacheBackend';
import { renderMarkdown } from './markdown';

const SEARCH_DEBOUNCE_MS = 350;

function formatCount(value) {
    const number = Number(value || 0);
    if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(1)}M`;
    if (number >= 1_000) return `${(number / 1_000).toFixed(1)}K`;
    return String(number);
}

// One row in the left-hand search results list.
function ResultRow({ item, active, onSelect }) {
    return (
        <button
            type="button"
            onClick={() => onSelect(item.repoId)}
            className={`w-full rounded-lg border px-3 py-2 text-left text-xs transition ${
                active
                    ? 'border-sky-400/60 bg-sky-400/10'
                    : 'border-transparent hover:border-slate-700 hover:bg-slate-800/50'
            }`}
        >
            <div className="truncate font-mono font-semibold text-slate-100">{item.repoId}</div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                {item.pipelineTag && <span className="rounded-full border border-slate-700 px-1.5 py-0.5">{item.pipelineTag}</span>}
                <span>❤ {formatCount(item.likes)}</span>
                <span>⬇ {formatCount(item.downloads)}</span>
            </div>
        </button>
    );
}

export function HuggingFaceModelPickerModal({ onCancel, onSelect }) {
    const [query, setQuery] = useState('');
    const [results, setResults] = useState([]);
    const [searching, setSearching] = useState(false);
    const [searchError, setSearchError] = useState('');
    const [selectedRepoId, setSelectedRepoId] = useState('');
    const [detail, setDetail] = useState(null);
    const [detailLoading, setDetailLoading] = useState(false);
    const [detailError, setDetailError] = useState('');
    const firstFieldRef = useRef(null);

    useEffect(() => {
        firstFieldRef.current?.focus();
    }, []);

    useEffect(() => {
        const needle = query.trim();
        if (!needle) {
            setResults([]);
            setSearchError('');
            setSearching(false);
            return undefined;
        }
        const controller = new AbortController();
        setSearching(true);
        const timer = setTimeout(async () => {
            try {
                const items = await searchHuggingFaceModels(needle, { signal: controller.signal });
                setResults(items);
                setSearchError('');
            } catch (failure) {
                if (controller.signal.aborted) return;
                setResults([]);
                setSearchError(errorMessage(failure, 'HuggingFace search failed'));
            } finally {
                if (!controller.signal.aborted) setSearching(false);
            }
        }, SEARCH_DEBOUNCE_MS);
        return () => {
            clearTimeout(timer);
            controller.abort();
        };
    }, [query]);

    useEffect(() => {
        if (!selectedRepoId) {
            setDetail(null);
            return undefined;
        }
        const controller = new AbortController();
        setDetail(null);
        setDetailLoading(true);
        setDetailError('');
        (async () => {
            try {
                const payload = await getHuggingFaceModelDetail(selectedRepoId, { signal: controller.signal });
                if (controller.signal.aborted) return;
                setDetail(payload);
            } catch (failure) {
                if (controller.signal.aborted) return;
                setDetail(null);
                setDetailError(errorMessage(failure, 'Failed to load model details'));
            } finally {
                if (!controller.signal.aborted) setDetailLoading(false);
            }
        })();
        return () => controller.abort();
    }, [selectedRepoId]);

    // "Use this model" only needs the repo id itself, which is already known
    // the instant a result row is clicked -- the right-hand README/tags panel
    // is supplementary preview info, not a precondition. Detail (pipelineTag/
    // tags) is included in the confirm payload opportunistically only if it
    // has already loaded for this exact selection; otherwise it's omitted
    // rather than blocking confirmation.
    const canConfirm = Boolean(selectedRepoId);

    const emptyState = useMemo(() => {
        if (searching) return null;
        if (searchError) return null;
        if (!query.trim()) return 'Search HuggingFace Hub by model name, e.g. "llama" or "qwen".';
        if (!results.length) return 'No models matched your search.';
        return null;
    }, [searching, searchError, query, results]);

    return (
        <Modal
            isOpen
            onClose={onCancel}
            title="Browse HuggingFace Hub"
            subtitle="Search public models, review the README and tags, then confirm to fill in the repo id."
            size="xl"
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel}>Cancel</Button>
                    <Button
                        variant="sky"
                        disabled={!canConfirm}
                        onClick={() => {
                            const detailMatchesSelection = detail?.repoId === selectedRepoId;
                            onSelect({
                                repoId: selectedRepoId,
                                revision: 'main',
                                pipelineTag: detailMatchesSelection ? (detail.pipelineTag || '') : '',
                                tags: detailMatchesSelection ? (detail.tags || []) : [],
                            });
                        }}
                    >
                        Use this model
                    </Button>
                </>
            }
        >
            <div className="grid grid-cols-1 gap-4 md:grid-cols-5">
                <div className="md:col-span-2">
                    <div className="relative">
                        <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                        <Input
                            ref={firstFieldRef}
                            value={query}
                            onChange={(event) => setQuery(event.target.value)}
                            placeholder="Search models…"
                            className="pl-8"
                        />
                    </div>
                    <div className="mt-3 flex h-[52vh] flex-col gap-1.5 overflow-y-auto custom-scrollbar pr-1">
                        {searching && (
                            <div className="flex items-center gap-2 px-1 py-2 text-xs text-slate-400">
                                <Loader2 size={14} className="animate-spin" /> Searching…
                            </div>
                        )}
                        {searchError && (
                            <p role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                                {searchError}
                            </p>
                        )}
                        {emptyState && <p className="px-1 py-2 text-xs text-slate-500">{emptyState}</p>}
                        {results.map((item) => (
                            <ResultRow
                                key={item.repoId}
                                item={item}
                                active={item.repoId === selectedRepoId}
                                onSelect={setSelectedRepoId}
                            />
                        ))}
                    </div>
                </div>

                <div className="md:col-span-3">
                    <div className="h-[58vh] overflow-y-auto custom-scrollbar rounded-lg border border-slate-800/60 bg-slate-950/40 p-3">
                        {!selectedRepoId && (
                            <p className="text-xs text-slate-500">Select a model on the left to preview its README and tags.</p>
                        )}
                        {selectedRepoId && detailLoading && (
                            <div className="flex items-center gap-2 text-xs text-slate-400">
                                <Loader2 size={14} className="animate-spin" /> Loading model details…
                            </div>
                        )}
                        {selectedRepoId && detailError && (
                            <p role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                                {detailError}
                            </p>
                        )}
                        {selectedRepoId && detail && detail.repoId === selectedRepoId && !detailLoading && !detailError && (
                            <div className="flex flex-col gap-3">
                                <div>
                                    <div className="font-mono text-sm font-semibold text-slate-100">{detail.repoId}</div>
                                    <div className="mt-1.5 flex flex-wrap gap-1.5">
                                        {detail.pipelineTag && (
                                            <span className="rounded-full border border-sky-400/30 bg-sky-400/10 px-2 py-0.5 text-[11px] text-sky-200">
                                                {detail.pipelineTag}
                                            </span>
                                        )}
                                        {(detail.tags || []).map((tag) => (
                                            <span key={tag} className="rounded-full border border-slate-700 px-2 py-0.5 text-[11px] text-slate-300">
                                                {tag}
                                            </span>
                                        ))}
                                    </div>
                                    <div className="mt-1.5 text-[11px] text-slate-400">
                                        ❤ {formatCount(detail.likes)} likes &nbsp;·&nbsp; ⬇ {formatCount(detail.downloads)} downloads
                                    </div>
                                </div>
                                {detail.readme ? (
                                    <div
                                        className="markdown-body rounded-lg border border-slate-800/60 bg-slate-900/60 p-3 text-[12px] leading-relaxed text-slate-300"
                                        dangerouslySetInnerHTML={{ __html: renderMarkdown(detail.readme) }}
                                    />
                                ) : (
                                    <p className="rounded-lg border border-slate-800/60 bg-slate-900/60 p-3 text-[12px] text-slate-500">
                                        No README available for this model.
                                    </p>
                                )}
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </Modal>
    );
}

export default HuggingFaceModelPickerModal;
