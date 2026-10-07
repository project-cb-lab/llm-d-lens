import { useResourceList } from '../../hooks/useResourceList';
import { useMemo, useState } from 'react';
import { usePolling } from '../../hooks/usePolling';
import { CheckCircle2, Loader2, Pencil, Plug, Plus, RefreshCw, Search, Trash2, XCircle } from 'lucide-react';
import { deleteAIProvider, listAIProviders, testSavedAIProvider } from './aiProvidersBackend';
import { errorMessage, providerTitle } from './aiProvidersPresentation';
import { AIProviderFormModal } from './AIProviderFormModal';
import { DeleteAIProviderModal } from './DeleteAIProviderModal';
import { EmptyState } from '../ui/EmptyState';
import { AsyncState } from '../shared/AsyncState';
import { Button } from '../ui/Button';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { MultiSelectDropdown } from '../common/MultiSelectDropdown';

// Manage external AI providers ("External providers") that power optional
// AI capabilities across Agentic components. Backed by /api/v1/ai-providers
// (llm_d_bench/ai_providers/router.py).
export function AIProvidersPage({ onToggleMobileNav }) {
    const [dialog, setDialog] = useState(null); // { mode: 'add' | 'edit', provider? }
    const [pendingDelete, setPendingDelete] = useState(null);
    const [testingId, setTestingId] = useState(null);
    const [testResults, setTestResults] = useState({});
    const [searchInput, setSearchInput] = useState('');
    const [typeFilters, setTypeFilters] = useState(new Set());
    const { items, loading, refreshing, error, load } = useResourceList(listAIProviders, {
        errorFallback: 'Failed to load AI providers', retainOnError: true,
    });

    usePolling(() => load({ quiet: true }));

    const runTest = async (provider) => {
        setTestingId(provider.id);
        setTestResults((prev) => ({ ...prev, [provider.id]: null }));
        try {
            const result = await testSavedAIProvider(provider.id);
            setTestResults((prev) => ({ ...prev, [provider.id]: result }));
        } catch (failure) {
            setTestResults((prev) => ({ ...prev, [provider.id]: { success: false, message: errorMessage(failure, 'Test failed') } }));
        } finally {
            setTestingId(null);
        }
    };

    const handleDelete = async () => {
        await deleteAIProvider(pendingDelete.id);
        setPendingDelete(null);
        await load();
    };

    const typeOptions = useMemo(() => (
        [...new Set(items.map((provider) => provider.providerType).filter(Boolean))]
    ), [items]);

    const filteredItems = useMemo(() => {
        const query = searchInput.trim().toLowerCase();
        return items.filter((provider) => {
            if (typeFilters.size && !typeFilters.has(provider.providerType)) return false;
            if (!query) return true;
            const haystack = [providerTitle(provider), provider.baseUrl, provider.model].join(' ').toLowerCase();
            return haystack.includes(query);
        });
    }, [items, searchInput, typeFilters]);

    const toggleTypeFilter = (value) => setTypeFilters((current) => {
        const next = new Set(current);
        if (next.has(value)) next.delete(value); else next.add(value);
        return next;
    });

    const clearFilters = () => { setSearchInput(''); setTypeFilters(new Set()); };
    const hasActiveFilters = Boolean(searchInput || typeFilters.size);

    return (
        <ModulePage>
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={Plug}
                    title="External providers"
                    description="Manage external AI providers that power optional AI capabilities across Agentic components."
                    onToggleMobileNav={onToggleMobileNav}
                    actions={
                        <>
                            <Button variant="secondary" size="sm" onClick={load} isLoading={refreshing} disabled={loading}>
                                <RefreshCw size={14} /> Refresh
                            </Button>
                            <Button variant="sky" size="sm" onClick={() => setDialog({ mode: 'add' })}>
                                <Plus size={14} /> Add provider
                            </Button>
                        </>
                    }
                />

                {error && items.length > 0 && (
                    <p role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                        {error}
                    </p>
                )}

                <AsyncState
                    loading={loading}
                    error={items.length === 0 ? error : null}
                    empty={items.length === 0}
                    onRetry={load}
                    emptyContent={<EmptyState
                        icon={<Plug size={22} />}
                        title="No AI providers configured"
                        message="Add an OpenAI-compatible endpoint (OpenAI, Azure OpenAI, a self-hosted vLLM/Ollama server, etc.) so Agentic components can use it."
                        action={<Button variant="sky" size="sm" onClick={() => setDialog({ mode: 'add' })}><Plus size={14} /> Add provider</Button>}
                    />}
                >
                    <section className="relative z-10 flex flex-col gap-3.5 rounded-3xl border border-slate-900/90 bg-[#070b13]/65 p-5 shadow-2xl backdrop-blur-md">
                        <div className="relative z-20 flex flex-wrap items-center gap-3 rounded-2xl border border-slate-800/60 bg-[#0a0f1d] p-3 shadow-md">
                            <div className="min-w-[240px] flex-1">
                                <div className="relative">
                                    <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                                    <input
                                        type="search"
                                        placeholder="Search name, base URL, or model..."
                                        value={searchInput}
                                        onChange={(event) => setSearchInput(event.target.value)}
                                        className="h-9 w-full rounded-xl border border-slate-800/60 bg-[#0b0f17] pl-9 pr-4 text-xs font-medium text-slate-200 outline-none focus:border-cyan-500/40"
                                    />
                                </div>
                            </div>
                            <div className="w-48 shrink-0">
                                <MultiSelectDropdown
                                    label="Type"
                                    options={typeOptions}
                                    selected={typeFilters}
                                    onChange={toggleTypeFilter}
                                    formatLabel={(value) => (value === 'anthropic' ? 'Anthropic' : 'OpenAI')}
                                />
                            </div>
                            {hasActiveFilters && (
                                <button onClick={clearFilters} className="h-8 rounded-lg border border-slate-700 px-3 text-[10px] text-slate-300">
                                    Clear Filters
                                </button>
                            )}
                        </div>

                        <div className="flex min-h-[52px] items-center justify-between border-b border-slate-800/60 px-1 py-2.5">
                            <div className="flex items-center gap-2">
                                <Plug className="h-4 w-4 text-cyan-400" />
                                <h2 className="text-sm font-bold text-slate-300">External providers</h2>
                                <span className="rounded-md bg-slate-900 px-2 py-0.5 text-[9px] text-slate-500">{filteredItems.length} shown</span>
                            </div>
                        </div>

                        {filteredItems.length === 0 ? (
                            <div className="rounded-2xl border border-slate-800 bg-slate-900/80 px-6 py-12 text-center">
                                <Plug className="mx-auto h-7 w-7 text-slate-600" />
                                <h3 className="mt-3 text-sm font-semibold">No matching providers</h3>
                                <p className="mt-2 text-xs text-slate-500">Adjust filters or add a new provider.</p>
                            </div>
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full min-w-[54rem] text-left text-sm">
                                    <thead className="bg-slate-950/60 text-xs uppercase tracking-wide text-slate-500">
                                        <tr>
                                            <th className="px-4 py-3">Name</th>
                                            <th className="px-4 py-3">Type</th>
                                            <th className="px-4 py-3">Base URL</th>
                                            <th className="px-4 py-3">Model</th>
                                            <th className="px-4 py-3">API key</th>
                                            <th className="px-4 py-3">Test</th>
                                            <th className="px-4 py-3 text-right">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {filteredItems.map((provider) => {
                                            const result = testResults[provider.id];
                                            const isTesting = testingId === provider.id;
                                            return (
                                                <tr key={provider.id} className="border-t border-slate-800">
                                                    <td className="px-4 py-3 font-medium text-slate-100">{providerTitle(provider)}</td>
                                                    <td className="px-4 py-3">
                                                        <span className="rounded-full border border-slate-700/60 bg-slate-900/60 px-2 py-0.5 text-[11px] uppercase tracking-wide text-slate-300">
                                                            {provider.providerType === 'anthropic' ? 'Anthropic' : 'OpenAI'}
                                                        </span>
                                                    </td>
                                                    <td className="max-w-[220px] truncate px-4 py-3 font-mono text-xs text-slate-300" title={provider.baseUrl}>{provider.baseUrl}</td>
                                                    <td className="px-4 py-3 text-slate-300">{provider.model}</td>
                                                    <td className="px-4 py-3 text-slate-400">
                                                        {provider.hasApiKey ? `...${(provider.apiKeyPreview || '').replace(/^\.+/, '')}` : <span className="text-slate-600">not set</span>}
                                                    </td>
                                                    <td className="px-4 py-3">
                                                        <div className="flex items-center gap-2">
                                                            <Button variant="secondary" size="xs" onClick={() => runTest(provider)} isLoading={isTesting}>
                                                                Test
                                                            </Button>
                                                            {result && !isTesting && (
                                                                <span className={`flex items-center gap-1 text-xs ${result.success ? 'text-emerald-300' : 'text-rose-300'}`} title={result.message}>
                                                                    {result.success ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                                                                </span>
                                                            )}
                                                        </div>
                                                    </td>
                                                    <td className="px-4 py-3">
                                                        <div className="flex items-center justify-end gap-1">
                                                            <Button variant="ghost" size="icon" onClick={() => setDialog({ mode: 'edit', provider })} aria-label={`Edit ${providerTitle(provider)}`}>
                                                                <Pencil size={14} />
                                                            </Button>
                                                            <Button variant="ghost" size="icon" onClick={() => setPendingDelete(provider)} aria-label={`Delete ${providerTitle(provider)}`}>
                                                                <Trash2 size={14} className="text-rose-400" />
                                                            </Button>
                                                        </div>
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </section>
                </AsyncState>
            </div>

            {dialog && (
                <AIProviderFormModal
                    provider={dialog.mode === 'edit' ? dialog.provider : null}
                    onCancel={() => setDialog(null)}
                    onSaved={() => { setDialog(null); load(); }}
                />
            )}

            {pendingDelete && (
                <DeleteAIProviderModal
                    provider={pendingDelete}
                    onCancel={() => setPendingDelete(null)}
                    onDelete={handleDelete}
                />
            )}
        </ModulePage>
    );
}

export default AIProvidersPage;
