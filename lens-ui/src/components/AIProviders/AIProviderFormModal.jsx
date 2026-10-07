import { FormError } from '../ui/FormError';
import { useSubmission } from '../../hooks/useSubmission';
import { useEffect, useRef, useState } from 'react';
import { CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Input, Label, Select } from '../ui/FormControls';
import { createAIProvider, testDraftAIProvider, updateAIProvider } from './aiProvidersBackend';
import { errorMessage } from './aiProvidersPresentation';

const FORM_ID = 'ai-provider-form';

function emptyForm() {
    return { name: '', baseUrl: '', model: '', apiKey: '', providerType: 'openai', timeoutSeconds: '30' };
}

// Create/edit an External provider (an OpenAI- or Anthropic-compatible
// endpoint). `provider` is null for "add", or an existing provider payload
// (from the list) for "edit" -- editing leaves the stored API key untouched
// unless the operator types a new one.
export function AIProviderFormModal({ provider, onCancel, onSaved }) {
    const isEdit = Boolean(provider);
    const [form, setForm] = useState(() => (provider
        ? {
            name: provider.name || '',
            baseUrl: provider.baseUrl || '',
            model: provider.model || '',
            apiKey: '',
            providerType: provider.providerType || 'openai',
            timeoutSeconds: String(provider.timeoutSeconds ?? 30),
        }
        : emptyForm()));
    const { pending: saving, error, setError, run } = useSubmission(`Failed to ${isEdit ? 'update' : 'add'} AI provider`, { keepPendingOnSuccess: true });
    const [testing, setTesting] = useState(false);
    const [testResult, setTestResult] = useState(null);
    const nameRef = useRef(null);

    useEffect(() => {
        nameRef.current?.focus();
    }, []);

    const setField = (field) => (event) => {
        setForm((prev) => ({ ...prev, [field]: event.target.value }));
        setTestResult(null);
    };

    const timeoutSeconds = Number(form.timeoutSeconds) || 30;

    const runTest = async () => {
        if (testing) return;
        setTesting(true);
        setTestResult(null);
        setError('');
        try {
            // An empty apiKey here means "unauthenticated request" -- the draft
            // test route always uses whatever is currently typed on the form.
            const result = await testDraftAIProvider({
                baseUrl: form.baseUrl.trim(),
                model: form.model.trim(),
                apiKey: form.apiKey,
                providerType: form.providerType,
                timeoutSeconds,
            });
            setTestResult(result);
        } catch (failure) {
            setTestResult({ success: false, message: errorMessage(failure, 'Test failed') });
        } finally {
            setTesting(false);
        }
    };

    const submit = async (event) => {
        event.preventDefault();
        if (saving) return;
        await run(async () => {
            if (isEdit) {
                const payload = {
                    name: form.name.trim(),
                    baseUrl: form.baseUrl.trim(),
                    model: form.model.trim(),
                    providerType: form.providerType,
                    timeoutSeconds,
                };
                // Omitted apiKey leaves the stored key unchanged; an explicit
                // empty string is how an operator clears it.
                if (form.apiKey) payload.apiKey = form.apiKey;
                await updateAIProvider(provider.id, payload);
            } else {
                await createAIProvider({
                    name: form.name.trim(),
                    baseUrl: form.baseUrl.trim(),
                    model: form.model.trim(),
                    apiKey: form.apiKey,
                    providerType: form.providerType,
                    timeoutSeconds,
                });
            }
            onSaved();
        });
    };

    return (
        <Modal
            isOpen
            onClose={saving ? undefined : onCancel}
            title={isEdit ? 'Edit AI provider' : 'Add AI provider'}
            subtitle="Connect an OpenAI- or Anthropic-compatible completions endpoint (OpenAI, Azure OpenAI, Anthropic, vLLM, Ollama, etc.)."
            closeOnBackdrop={!saving}
            closeOnEscape={!saving}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel} disabled={saving}>Cancel</Button>
                    <Button type="submit" form={FORM_ID} variant="primary" isLoading={saving}>
                        {isEdit ? 'Save changes' : 'Add provider'}
                    </Button>
                </>
            }
        >
            <form id={FORM_ID} onSubmit={submit} className="flex flex-col gap-4">
                <div>
                    <Label htmlFor="ai-provider-name">Name</Label>
                    <Input
                        ref={nameRef}
                        id="ai-provider-name"
                        value={form.name}
                        onChange={setField('name')}
                        placeholder="e.g. Production OpenAI"
                        required
                        maxLength={200}
                    />
                </div>
                <div>
                    <Label htmlFor="ai-provider-type">API type</Label>
                    <Select id="ai-provider-type" value={form.providerType} onChange={setField('providerType')}>
                        <option value="openai">OpenAI-compatible (chat completions)</option>
                        <option value="anthropic">Anthropic-compatible (messages)</option>
                    </Select>
                </div>
                <div>
                    <Label htmlFor="ai-provider-base-url">Base URL</Label>
                    <Input
                        id="ai-provider-base-url"
                        type="url"
                        value={form.baseUrl}
                        onChange={setField('baseUrl')}
                        placeholder={form.providerType === 'anthropic' ? 'https://api.anthropic.com/' : 'https://api.openai.com/'}
                        required
                    />
                </div>
                <div>
                    <Label htmlFor="ai-provider-model">Model</Label>
                    <Input
                        id="ai-provider-model"
                        value={form.model}
                        onChange={setField('model')}
                        placeholder={form.providerType === 'anthropic' ? 'e.g. claude-3-5-sonnet' : 'e.g. gpt-4o-mini'}
                        required
                        maxLength={200}
                    />
                </div>
                <div>
                    <Label htmlFor="ai-provider-api-key">
                        API key {isEdit && <span className="font-normal text-slate-500">({provider.hasApiKey ? `saved, ending ${provider.apiKeyPreview || '...'}` : 'not set'} — leave blank to keep)</span>}
                    </Label>
                    <Input
                        id="ai-provider-api-key"
                        type="password"
                        value={form.apiKey}
                        onChange={setField('apiKey')}
                        placeholder={isEdit ? 'Leave blank to keep the saved key' : (form.providerType === 'anthropic' ? 'sk-ant-...' : 'sk-...')}
                        autoComplete="new-password"
                    />
                </div>
                <div>
                    <Label htmlFor="ai-provider-timeout">Timeout (seconds)</Label>
                    <Input
                        id="ai-provider-timeout"
                        type="number"
                        min={1}
                        max={120}
                        value={form.timeoutSeconds}
                        onChange={setField('timeoutSeconds')}
                    />
                </div>

                <div className="flex flex-col gap-2 rounded-lg border border-slate-700/60 bg-slate-800/40 px-3 py-2.5">
                    <div className="flex items-center gap-3">
                        <Button
                            type="button"
                            variant="secondary"
                            size="xs"
                            onClick={runTest}
                            isLoading={testing}
                            disabled={!form.baseUrl.trim() || !form.model.trim()}
                        >
                            Test connection
                        </Button>
                        <span className="text-[11px] text-slate-500">Optional — you can save even if this fails.</span>
                    </div>
                    {testResult && (
                        <div className={`flex items-start gap-1.5 text-xs ${testResult.success ? 'text-emerald-300' : 'text-rose-300'}`}>
                            <span className="mt-0.5 shrink-0">
                                {testing ? <Loader2 size={14} className="animate-spin" /> : testResult.success ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                            </span>
                            <span className="whitespace-pre-wrap break-words">{testResult.message}</span>
                        </div>
                    )}
                </div>

                <FormError message={error} />
            </form>
        </Modal>
    );
}

export default AIProviderFormModal;
