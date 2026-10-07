import { useMemo, useState } from 'react';
import { Search } from 'lucide-react';
import { Modal } from '../ui/Modal';
import { Input } from '../ui/FormControls';
import { CATEGORIES, MODELS } from '../../data/modelCatalog';
import { ModelCard } from '../ModelMarketPage';

export function ModelMarketPickerModal({ onCancel, onSelect }) {
    const [category, setCategory] = useState('All');
    const [search, setSearch] = useState('');

    const visibleModels = useMemo(() => {
        const needle = search.trim().toLowerCase();
        return MODELS.filter((model) => {
            if (category !== 'All' && model.category !== category) return false;
            if (!needle) return true;
            return [model.name, model.family, model.repository, model.type]
                .some((item) => String(item || '').toLowerCase().includes(needle));
        });
    }, [category, search]);

    return (
        <Modal
            isOpen
            onClose={onCancel}
            title="Browse Model Market"
            subtitle="Select a model from Model Market to download into your storage volume."
            size="2xl"
        >
            <div className="flex flex-col gap-4">
                <div className="flex items-center gap-3">
                    <div className="relative flex-1">
                        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                        <Input
                            value={search}
                            onChange={(event) => setSearch(event.target.value)}
                            placeholder="Search by model name, family, or repo..."
                            className="pl-9 text-xs"
                        />
                    </div>
                    <span className="shrink-0 text-xs text-slate-500">{visibleModels.length} models</span>
                </div>

                <div className="flex flex-wrap gap-2" role="tablist" aria-label="Model categories">
                    {CATEGORIES.map((item) => (
                        <button
                            key={item}
                            type="button"
                            role="tab"
                            aria-selected={category === item}
                            onClick={() => setCategory(item)}
                            className={`rounded-md border px-3 py-1.5 text-xs transition ${
                                category === item
                                    ? 'border-cyan-400 bg-cyan-400/10 text-cyan-200'
                                    : 'border-slate-700 text-slate-400 hover:border-slate-500'
                            }`}
                        >
                            {item}
                        </button>
                    ))}
                </div>

                <div className="max-h-[60vh] overflow-y-auto pr-1">
                    {visibleModels.length > 0 ? (
                        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
                            {visibleModels.map((model) => (
                                <ModelCard key={model.id} model={model} onSelect={onSelect} />
                            ))}
                        </div>
                    ) : (
                        <p className="py-8 text-center text-xs text-slate-500">No models found matching "{search}"</p>
                    )}
                </div>
            </div>
        </Modal>
    );
}

export default ModelMarketPickerModal;
