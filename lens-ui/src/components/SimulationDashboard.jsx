import { FALLBACK_SCENARIOS, RUNNING_STATUSES, STOPPABLE_STATUSES, arrayFrom, scenarioOptions, unwrapTask, formatNumber, formatMilliseconds, formatBytes, displayName, taskBackend, taskDataset, taskStatusChip, taskMetrics, scenarioDisplayName } from '../features/simulation/presentation';
import { requestJson } from '../features/simulation/client';

/* eslint-disable react-refresh/only-export-components */
import { PaginationControls } from './ui/PaginationControls';
// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
    Area,
    AreaChart,
    Brush,
    CartesianGrid,
    ComposedChart,
    Line,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import {
    Activity,
    AlertTriangle,
    ArrowRight,
    ChevronLeft,
    ChevronRight,
    Clock3,
    Download,
    FileJson,
    Gauge,
    Info,
    MessageSquareText,
    Plus,
    Play,
    RefreshCw,
    Search,
    Server,
    SlidersHorizontal,
    Square,
    Trash2,
    X,
} from 'lucide-react';
import {
    Badge,
    Button,
    EmptyState,
    Input,
    Label,
    Modal,
    ModuleHeader,
    ModulePage,
    Panel,
    Select,
    Spinner,
    StatusChip,
    Textarea,
    ToggleGroup,
} from './ui';
import { MultiSelectDropdown } from './common';
import { listModelNames } from './ModelService/modelServiceBackend';
import SimulationIcon from './SimulationIcon';

const EMPTY_ADVANCED_FILTERS = {
    backend: '',
    model: '',
    dataset: '',
    traceFormat: '',
    endpoint: '',
    createdAfter: '',
    createdBefore: '',
};

const TASK_WIZARD_STEPS = [
    'Benchmark & Endpoint',
    'Scenario & Prompt',
    'Load & Backend',
    'Task Details',
];

const AIPERF_OPTION_FORM_KEYS = {
    random_seed: 'randomSeed',
    num_profile_runs: 'numProfileRuns',
    profile_run_cooldown_seconds: 'profileRunCooldownSeconds',
    concurrency: 'aiperfConcurrency',
    max_context_length: 'maxContextLength',
    trace_idle_gap_cap_seconds: 'traceIdleGapCapSeconds',
};

function createShortId() {
    return globalThis.crypto?.randomUUID?.().slice(0, 8)
        || Date.now().toString(36).slice(-8);
}

export default function SimulationDashboard({ onNavigate, onToggleMobileNav, embedded = false }) {
    const [backends, setBackends] = useState([]);
    const [scenarios, setScenarios] = useState(FALLBACK_SCENARIOS);
    const [datasets, setDatasets] = useState([]);
    const [catalogLoading, setCatalogLoading] = useState(true);
    const [catalogError, setCatalogError] = useState('');
    const [errors, setErrors] = useState([]);
    const [actionError, setActionError] = useState('');
    const [datasetDownloadError, setDatasetDownloadError] = useState('');
    const [downloading, setDownloading] = useState(false);
    const [timeline, setTimeline] = useState(null);
    const [timelineLoading, setTimelineLoading] = useState(false);
    const [timelineError, setTimelineError] = useState('');
    const [timelineSelection, setTimelineSelection] = useState({ startIndex: 0, endIndex: 0 });
    const [modelServices, setModelServices] = useState([]);
    const [modelServicesLoading, setModelServicesLoading] = useState(false);
    const [modelServicesError, setModelServicesError] = useState('');
    const [endpointModels, setEndpointModels] = useState([]);
    const [modelsLoading, setModelsLoading] = useState(false);
    const [modelDiscoveryError, setModelDiscoveryError] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [stoppingTaskId, setStoppingTaskId] = useState('');
    const [rerunningTaskId, setRerunningTaskId] = useState('');
    const [deletingTaskId, setDeletingTaskId] = useState('');
    const [taskPendingDelete, setTaskPendingDelete] = useState(null);
    const [deleteError, setDeleteError] = useState('');
    const [tasks, setTasks] = useState([]);
    const [total, setTotal] = useState(0);
    const [counts, setCounts] = useState({
        queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0,
    });
    const [listLoading, setListLoading] = useState(true);
    const [page, setPage] = useState(0);
    const [pageSize, setPageSize] = useState(7);
    const [statusFilter, setStatusFilter] = useState('');
    const [searchInput, setSearchInput] = useState('');
    const [search, setSearch] = useState('');
    const [advancedFilters, setAdvancedFilters] = useState(EMPTY_ADVANCED_FILTERS);
    const [draftAdvancedFilters, setDraftAdvancedFilters] = useState(EMPTY_ADVANCED_FILTERS);
    const [isAdvancedOpen, setIsAdvancedOpen] = useState(false);
    const [facets, setFacets] = useState({
        backends: [], models: [], datasets: [], trace_formats: [], endpoints: [],
    });
    const [isNewTaskOpen, setIsNewTaskOpen] = useState(false);
    const [wizardStep, setWizardStep] = useState(0);
    const [taskNameSuffix, setTaskNameSuffix] = useState('');
    const modelDiscoveryRequestRef = useRef(0);
    const [form, setForm] = useState({
        scenario: 'chat',
        backend: 'aiperf',
        dataset: '',
        tracePath: '',
        traceFormat: '',
        endpointMode: 'deployment',
        endpointModelServiceId: '',
        endpointUrl: '',
        apiKey: '',
        modelName: '',
        runName: '',
        description: '',
        durationSeconds: '60',
        scaleFactor: '1',
        traceStartSeconds: '0',
        traceEndSeconds: '',
        traceTimeoutSeconds: '3600',
        numProducer: '16',
        channelCapacity: '32',
        threads: '32',
        ttftSlo: '',
        tpotSlo: '',
        errorRateSlo: '',
        earlyStopErrorThreshold: '',
        randomSeed: '',
        numProfileRuns: '1',
        profileRunCooldownSeconds: '',
        aiperfConcurrency: '1',
        maxContextLength: '',
        traceIdleGapCapSeconds: '',
    });

    const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));

    const resetModelDiscovery = useCallback(() => {
        modelDiscoveryRequestRef.current += 1;
        setEndpointModels([]);
        setModelDiscoveryError('');
        setModelsLoading(false);
    }, []);

    const chooseEndpointMode = (value) => {
        setModelServicesError('');
        resetModelDiscovery();
        setForm((current) => ({
            ...current,
            endpointMode: value,
            endpointModelServiceId: '',
            endpointUrl: '',
            modelName: '',
        }));
        if (value === 'deployment') void loadModelServices();
    };

    const discoverEndpointModels = useCallback(async (endpointUrl) => {
        const requestId = ++modelDiscoveryRequestRef.current;
        setModelsLoading(true);
        setModelDiscoveryError('');
        setEndpointModels([]);
        try {
            const payload = await requestJson('/api/v1/simulation/models/discover', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ endpoint_url: endpointUrl }),
            });
            const models = Array.isArray(payload?.models) ? payload.models : [];
            if (!models.length) throw new Error('The endpoint reported no models.');
            if (modelDiscoveryRequestRef.current !== requestId) return;
            setEndpointModels(models);
            setForm((current) => (
                current.endpointUrl === endpointUrl
                    ? { ...current, modelName: models.length === 1 ? models[0] : '' }
                    : current
            ));
        } catch (error) {
            if (modelDiscoveryRequestRef.current !== requestId) return;
            setModelDiscoveryError(`${error.message} Enter the model name manually.`);
        } finally {
            if (modelDiscoveryRequestRef.current === requestId) setModelsLoading(false);
        }
    }, []);

    useEffect(() => {
        if (form.endpointMode === 'deployment') return undefined;
        const endpointUrl = form.endpointUrl.trim();
        if (!endpointUrl) {
            resetModelDiscovery();
            return undefined;
        }
        try {
            const endpoint = new URL(endpointUrl);
            if (!['http:', 'https:'].includes(endpoint.protocol)) return undefined;
        } catch {
            return undefined;
        }
        const timeout = window.setTimeout(() => {
            void discoverEndpointModels(endpointUrl);
        }, 500);
        return () => window.clearTimeout(timeout);
    }, [discoverEndpointModels, form.endpointMode, form.endpointUrl, resetModelDiscovery]);

    const updateExternalEndpoint = (value) => {
        resetModelDiscovery();
        setForm((current) => ({
            ...current,
            endpointUrl: value,
            modelName: '',
        }));
    };

    const loadModelServices = async () => {
        setModelServicesLoading(true);
        setModelServicesError('');
        try {
            const items = await listModelNames();
            setModelServices(items);
            resetModelDiscovery();
            if (!items.length) {
                setForm((current) => ({ ...current, endpointModelServiceId: '', endpointUrl: '', modelName: '' }));
                setModelServicesError('No model service is published and healthy yet. Publish one from the Model Service page first.');
                return;
            }
            selectModelService(items[0].id, items);
        } catch (error) {
            setModelServices([]);
            setModelServicesError(`Unable to load model services: ${error.message}`);
        } finally {
            setModelServicesLoading(false);
        }
    };

    const selectModelService = (modelServiceId, items = modelServices) => {
        setModelServicesError('');
        resetModelDiscovery();
        const entry = items.find((candidate) => candidate.id === modelServiceId);
        if (!entry) {
            setForm((current) => ({ ...current, endpointModelServiceId: '', endpointUrl: '', modelName: '' }));
            return;
        }
        // The backend resolves the actual in-cluster endpoint from the published
        // Model Service (always the first healthy member); this placeholder URL
        // satisfies request validation and is discarded once the server re-resolves it.
        const modelName = entry.name || entry.baseModel || '';
        setForm((current) => ({
            ...current,
            endpointModelServiceId: modelServiceId,
            endpointUrl: 'http://model-service.internal',
            modelName,
        }));
        if (modelName) setEndpointModels([modelName]);
    };

    const loadTasks = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet) setListLoading(true);
        try {
            const query = new URLSearchParams({
                limit: String(pageSize),
                offset: String(page * pageSize),
            });
            if (statusFilter) query.set('status', statusFilter);
            if (search) query.set('search', search);
            if (advancedFilters.backend) query.set('backend', advancedFilters.backend);
            if (advancedFilters.model) query.set('model', advancedFilters.model);
            if (advancedFilters.dataset) query.set('dataset', advancedFilters.dataset);
            if (advancedFilters.traceFormat) query.set('trace_format', advancedFilters.traceFormat);
            if (advancedFilters.endpoint.trim()) query.set('endpoint', advancedFilters.endpoint.trim());
            if (advancedFilters.createdAfter) query.set('created_after', `${advancedFilters.createdAfter}T00:00:00.000Z`);
            if (advancedFilters.createdBefore) query.set('created_before', `${advancedFilters.createdBefore}T23:59:59.999Z`);
            const payload = await requestJson(`/api/v1/simulation/tasks?${query.toString()}`);
            const nextTasks = arrayFrom(payload, ['tasks', 'items', 'data']);
            setTasks(nextTasks);
            const nextTotal = Number(payload?.total ?? nextTasks.length);
            setTotal(nextTotal);
            if (page > 0 && nextTasks.length === 0 && nextTotal > 0) {
                setPage(Math.max(0, Math.ceil(nextTotal / pageSize) - 1));
            }
            setCounts({
                queued: Number(payload?.counts?.queued ?? nextTasks.filter((item) => item.status === 'queued').length),
                running: Number(payload?.counts?.running ?? nextTasks.filter((item) => item.status === 'running').length),
                completed: Number(payload?.counts?.completed ?? nextTasks.filter((item) => item.status === 'completed').length),
                failed: Number(payload?.counts?.failed ?? nextTasks.filter((item) => item.status === 'failed').length),
                cancelled: Number(payload?.counts?.cancelled ?? nextTasks.filter((item) => ['cancelled', 'canceled', 'stopped'].includes(item.status)).length),
            });
            setFacets(payload?.facets || {
                backends: [], models: [], datasets: [], trace_formats: [], endpoints: [],
            });
        } catch (error) {
            setActionError(`Unable to load simulation tasks: ${error.message}`);
        } finally {
            if (!quiet) setListLoading(false);
        }
    }, [advancedFilters, page, pageSize, search, statusFilter]);

    const openTaskDetails = (item) => {
        const id = item?.id || item?.task_id;
        if (!id) return;
        onNavigate?.('optimization-simulate-details', { taskId: id });
    };

    const loadCatalogs = useCallback(async () => {
        setCatalogLoading(true);
        setCatalogError('');
        const results = await Promise.allSettled([
            requestJson('/api/v1/simulation/backends'),
            requestJson('/api/v1/simulation/scenarios'),
            requestJson('/api/v1/simulation/trace-datasets'),
        ]);
        const messages = [];
        if (results[0].status === 'fulfilled') {
            const items = arrayFrom(results[0].value, ['backends', 'items', 'data']);
            setBackends(items);
            setForm((current) => ({
                ...current,
                backend: items.some((item) => (
                    (item.name || item.id || item.value) === current.backend
                ))
                    ? current.backend
                    : items.some((item) => (
                        (item.name || item.id || item.value) === 'aiperf'
                    ))
                        ? 'aiperf'
                        : (items[0]?.name || items[0]?.id || items[0]?.value || ''),
                scenario: current.scenario || 'chat',
            }));
        } else {
            messages.push(`Backends: ${results[0].reason.message}`);
        }
        if (results[1].status === 'fulfilled') {
            setScenarios(scenarioOptions(results[1].value));
        } else {
            setScenarios(FALLBACK_SCENARIOS);
            messages.push(`Scenarios: ${results[1].reason.message}`);
        }
        if (results[2].status === 'fulfilled') {
            setDatasets(arrayFrom(results[2].value, ['datasets', 'items', 'data']));
        } else {
            messages.push(`Trace datasets: ${results[2].reason.message}`);
        }
        setCatalogError(messages.join(' · '));
        setCatalogLoading(false);
    }, []);

    useEffect(() => {
        loadCatalogs();
    }, [loadCatalogs]);

    useEffect(() => {
        void loadModelServices();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    useEffect(() => {
        const timeout = window.setTimeout(() => {
            setPage(0);
            setSearch(searchInput.trim());
        }, 250);
        return () => window.clearTimeout(timeout);
    }, [searchInput]);

    useEffect(() => {
        loadTasks();
    }, [loadTasks]);

    const hasActiveTasks = counts.queued > 0 || counts.running > 0
        || tasks.some((item) => RUNNING_STATUSES.has(String(item.status || '').toLowerCase()));

    useEffect(() => {
        if (!hasActiveTasks) return undefined;
        let active = true;
        let timeout;
        const poll = async () => {
            if (!active) return;
            await loadTasks({ quiet: true });
            if (active) timeout = window.setTimeout(poll, 1000);
        };
        void poll();
        return () => {
            active = false;
            if (timeout) window.clearTimeout(timeout);
        };
    }, [hasActiveTasks, loadTasks]);

    const selectedDataset = useMemo(
        () => datasets.find((item) => (item.name || item.id || item.value) === form.dataset),
        [datasets, form.dataset]
    );
    const selectedBackend = useMemo(
        () => backends.find((item) => (item.name || item.id || item.value) === form.backend),
        [backends, form.backend]
    );
    const selectedModelService = useMemo(
        () => modelServices.find((item) => item.id === form.endpointModelServiceId),
        [modelServices, form.endpointModelServiceId]
    );
    const incompatibleDeploymentGuideError = (
        form.endpointMode === 'deployment'
        && selectedModelService?.guide
        && (selectedBackend?.capabilities?.incompatible_deployment_guides || []).includes(selectedModelService.guide)
    )
        ? `${selectedBackend?.display_name || displayName(form.backend)} does not support the "${selectedModelService.guide}" deployment guide. Choose a different model service or backend.`
        : '';
    const traceFormatCapabilities = (
        selectedBackend?.capabilities?.trace_format_capabilities?.[form.traceFormat] || {}
    );
    const supportsTraceRange = traceFormatCapabilities.supports_trace_range
        ?? selectedBackend?.capabilities?.supports_trace_range === true;
    const traceRangeMode = traceFormatCapabilities.trace_range_mode
        ?? (supportsTraceRange ? 'start_end' : 'none');
    const usesDurationRange = traceRangeMode === 'duration';
    const scaleFactorTraceFormats = selectedBackend?.capabilities?.scale_factor_trace_formats || [];
    const supportsScaleFactor = traceFormatCapabilities.supports_scale_factor
        ?? scaleFactorTraceFormats.includes(form.traceFormat);
    const isPublicDataset = selectedDataset?.source_type === 'external_dataset';
    const aiperfAdvancedOptions = (
        selectedBackend?.capabilities?.advanced_options || []
    ).filter((option) => (
        !Array.isArray(option.trace_formats) || option.trace_formats.includes(form.traceFormat)
    ));
    const availableScenarios = useMemo(() => {
        const supported = new Set((selectedBackend?.scenarios || []).map((scenario) => (
            scenario.name || scenario.value || scenario.id
        )));
        return supported.size ? scenarios.filter((scenario) => supported.has(scenario.value)) : scenarios;
    }, [scenarios, selectedBackend]);
    const compatibleDatasets = useMemo(() => datasets.filter((dataset) => {
        const supportedBackends = dataset.supported_backends;
        return dataset.scenario === form.scenario
            && (!Array.isArray(supportedBackends) || supportedBackends.includes(form.backend));
    }), [datasets, form.backend, form.scenario]);
    const timelinePoints = useMemo(() => (timeline?.bins || []).map((bin) => ({
        ...bin,
        time_seconds: (bin.start_seconds + bin.end_seconds) / 2,
    })), [timeline]);
    const timelineChartPoints = useMemo(() => (
        usesDurationRange
            ? timelinePoints.slice(0, timelineSelection.endIndex + 1)
            : timelinePoints
    ), [timelinePoints, timelineSelection.endIndex, usesDurationRange]);
    const timelineChartEnd = usesDurationRange && Number(form.traceEndSeconds) > 0
        ? Number(form.traceEndSeconds)
        : null;
    const selectedTimelineRequests = useMemo(() => (
        timelinePoints
            .slice(timelineSelection.startIndex, timelineSelection.endIndex + 1)
            .reduce((totalRequests, point) => totalRequests + point.request_count, 0)
    ), [timelinePoints, timelineSelection]);

    useEffect(() => {
        let active = true;
        if (!form.dataset || selectedDataset?.downloaded !== true || isPublicDataset) {
            setTimeline(null);
            setTimelineError('');
            setTimelineLoading(false);
            return () => {
                active = false;
            };
        }
        const datasetName = form.dataset;
        setTimeline(null);
        setTimelineError('');
        setTimelineLoading(true);
        requestJson(`/api/v1/simulation/trace-datasets/${encodeURIComponent(datasetName)}/timeline?bins=800`)
            .then((payload) => {
                if (!active) return;
                const bins = Array.isArray(payload?.bins) ? payload.bins : [];
                if (!bins.length || !(Number(payload.duration_seconds) > 0)) {
                    throw new Error('The dataset timeline is empty.');
                }
                const duration = Number(payload.duration_seconds);
                setTimeline(payload);
                setTimelineSelection({ startIndex: 0, endIndex: bins.length - 1 });
                setForm((current) => {
                    if (current.dataset !== datasetName) return current;
                    const scale = Number(current.scaleFactor);
                    const effectiveScale = supportsScaleFactor && Number.isFinite(scale) && scale > 0
                        ? scale : 1;
                    return {
                        ...current,
                        scaleFactor: supportsScaleFactor ? current.scaleFactor : '1',
                        traceStartSeconds: '0',
                        traceEndSeconds: supportsTraceRange ? String(duration) : '',
                        durationSeconds: supportsTraceRange
                            ? String(Math.max(1, Math.ceil(duration / effectiveScale)))
                            : current.durationSeconds,
                    };
                });
            })
            .catch((error) => {
                if (active) setTimelineError(`Unable to load dataset timeline: ${error.message}`);
            })
            .finally(() => {
                if (active) setTimelineLoading(false);
            });
        return () => {
            active = false;
        };
    }, [
        form.dataset,
        isPublicDataset,
        selectedDataset?.downloaded,
        supportsScaleFactor,
        supportsTraceRange,
    ]);

    const updateTimelineSelection = ({ startIndex, endIndex }) => {
        if (
            !supportsTraceRange
            ||
            !timelinePoints.length
            || !Number.isInteger(startIndex)
            || !Number.isInteger(endIndex)
        ) return;
        const effectiveStartIndex = usesDurationRange ? 0 : startIndex;
        const start = usesDurationRange ? 0 : timelinePoints[effectiveStartIndex].start_seconds;
        const end = timelinePoints[endIndex].end_seconds;
        const scale = Number(form.scaleFactor);
        setTimelineSelection({ startIndex: effectiveStartIndex, endIndex });
        setForm((current) => ({
            ...current,
            traceStartSeconds: String(start),
            traceEndSeconds: String(end),
            durationSeconds: String(Math.max(1, Math.ceil((end - start) / scale))),
        }));
    };

    const updateScaleFactor = (value) => {
        if (!supportsScaleFactor) return;
        const scale = Number(value);
        setForm((current) => {
            const start = Number(current.traceStartSeconds);
            const end = Number(current.traceEndSeconds);
            return {
                ...current,
                scaleFactor: value,
                durationSeconds: Number.isFinite(scale) && scale > 0 && end > start
                    ? String(Math.max(1, Math.ceil((end - start) / scale)))
                    : current.durationSeconds,
            };
        });
    };

    const updateDuration = (value) => {
        if (!supportsTraceRange) {
            update('durationSeconds', value);
            return;
        }
        const requestedDuration = Number(value);
        const scale = Number(form.scaleFactor);
        const start = Number(form.traceStartSeconds);
        if (
            !timelinePoints.length
            || !Number.isFinite(requestedDuration)
            || requestedDuration < 1
            || !Number.isFinite(scale)
            || scale <= 0
            || !Number.isFinite(start)
        ) {
            update('durationSeconds', value);
            return;
        }
        const maximumEnd = Number(timeline.duration_seconds);
        const end = Math.min(maximumEnd, start + requestedDuration * scale);
        const endIndex = timelinePoints.findIndex((point) => point.end_seconds >= end);
        const boundedEndIndex = endIndex < 0 ? timelinePoints.length - 1 : endIndex;
        setTimelineSelection((current) => ({
            startIndex: current.startIndex,
            endIndex: Math.max(current.startIndex, boundedEndIndex),
        }));
        setForm((current) => ({
            ...current,
            traceEndSeconds: String(end),
            durationSeconds: String(Math.max(1, Math.ceil((end - start) / scale))),
        }));
    };

    const chooseDataset = (value) => {
        setDatasetDownloadError('');
        setTimeline(null);
        setTimelineError('');
        const dataset = datasets.find((item) => (item.name || item.id || item.value) === value);
        setForm((current) => ({
            ...current,
            dataset: value,
            tracePath: dataset?.downloaded ? (dataset.path || dataset.file_path || '') : '',
            traceFormat: dataset?.trace_format || dataset?.format || current.traceFormat,
            scaleFactor: (
                selectedBackend?.capabilities?.scale_factor_trace_formats || []
            ).includes(dataset?.trace_format || dataset?.format) ? current.scaleFactor : '1',
            traceStartSeconds: '0',
            traceEndSeconds: '',
        }));
    };

    const chooseScenario = (value) => {
        setDatasetDownloadError('');
        setForm((current) => ({
            ...current,
            scenario: value,
            dataset: '',
            tracePath: '',
            traceFormat: '',
            scaleFactor: '1',
            traceStartSeconds: '0',
            traceEndSeconds: '',
        }));
    };

    const chooseBackend = (value) => {
        setDatasetDownloadError('');
        setTimeline(null);
        setTimelineError('');
        const backend = backends.find((item) => (
            (item.name || item.id || item.value) === value
        ));
        const supportedScenarios = (backend?.scenarios || []).map((scenario) => (
            scenario.name || scenario.value || scenario.id
        ));
        setForm((current) => ({
            ...current,
            backend: value,
            scenario: supportedScenarios.includes(current.scenario)
                ? current.scenario
                : supportedScenarios[0] || current.scenario,
            dataset: '',
            tracePath: '',
            traceFormat: '',
            scaleFactor: '1',
            traceStartSeconds: '0',
            traceEndSeconds: '',
        }));
    };

    const downloadDataset = async () => {
        if (!form.dataset) {
            setErrors(['Choose a trace dataset before downloading it.']);
            return;
        }
        setDownloading(true);
        setDatasetDownloadError('');
        try {
            const payload = await requestJson('/api/v1/simulation/trace-datasets/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ dataset: form.dataset, force: selectedDataset?.downloaded === true }),
            });
            const result = payload?.dataset || payload?.data || payload;
            setForm((current) => ({
                ...current,
                tracePath: result.path || result.trace_path || current.tracePath,
                traceFormat: result.trace_format || result.format || current.traceFormat,
            }));
            await loadCatalogs();
        } catch (error) {
            setDatasetDownloadError(`Dataset download failed: ${error.message}`);
        } finally {
            setDownloading(false);
        }
    };

    const validationErrorsForStep = (step) => {
        const next = [];
        if (step === 0) {
            if (!form.backend) next.push('Choose a simulation backend.');
            if (selectedBackend?.available === false) next.push('The selected backend is unavailable.');
            if (!form.endpointUrl.trim()) {
                next.push(form.endpointMode === 'deployment'
                    ? 'Select a published Model Service.'
                    : 'Endpoint URL is required.');
            }
            else {
                try {
                    const endpoint = new URL(form.endpointUrl);
                    if (!['http:', 'https:'].includes(endpoint.protocol)) throw new Error();
                } catch {
                    next.push('Endpoint URL must be a valid HTTP or HTTPS URL.');
                }
            }
            if (form.endpointMode === 'deployment' && !form.apiKey.trim()) {
                next.push('A model access token is required to call a Model Service through the cluster\'s shared Gateway.');
            }
            if (!form.modelName.trim()) next.push('Model name is required.');
            if (incompatibleDeploymentGuideError) next.push(incompatibleDeploymentGuideError);
            if (form.backend === 'aiperf') {
                aiperfAdvancedOptions.forEach((option) => {
                    const value = form[AIPERF_OPTION_FORM_KEYS[option.name]];
                    if (value === '') return;
                    const numericValue = Number(value);
                    if (!Number.isFinite(numericValue)) {
                        next.push(`${option.label} must be a number.`);
                    } else if (option.minimum !== undefined && numericValue < option.minimum) {
                        next.push(`${option.label} must be at least ${option.minimum}.`);
                    } else if (option.maximum !== undefined && numericValue > option.maximum) {
                        next.push(`${option.label} must be at most ${option.maximum}.`);
                    }
                });
            }
        }
        if (step === 1) {
            if (!form.scenario) next.push('Choose a Chat, Tool & API Use, or Code Generation scenario.');
            if (!form.dataset) next.push('Choose a trace dataset.');
            if (form.dataset && selectedDataset?.downloaded !== true) {
                next.push('Download the selected dataset before continuing.');
            } else if (!form.tracePath) {
                next.push('The downloaded dataset path is unavailable.');
            }
            if (!isPublicDataset) {
                if (timelineLoading) next.push('Wait for the dataset timeline to finish loading.');
                else if (timelineError) next.push(timelineError);
                else if (!timeline) next.push('The dataset timeline is unavailable.');
                if (supportsTraceRange && !(Number(form.traceEndSeconds) > Number(form.traceStartSeconds))) {
                    next.push('Select a non-empty replay interval on the workload timeline.');
                }
            }
            if (!(Number(form.scaleFactor) > 0)) next.push('Scale factor must be greater than zero.');
            if (!(Number(form.durationSeconds) >= 1)) next.push('Duration must be at least one second.');
            if (!supportsScaleFactor && Number(form.scaleFactor) !== 1) {
                next.push(`${selectedBackend?.display_name || displayName(form.backend)} does not support a scale factor for this trace format.`);
            }
        }
        if (step === 2) {
            if (!(Number(form.traceTimeoutSeconds) >= 1)) next.push('Trace timeout must be at least one second.');
            [
                ['TTFT SLO', form.ttftSlo],
                ['TPOT SLO', form.tpotSlo],
            ].forEach(([label, value]) => {
                if (value !== '' && (!Number.isFinite(Number(value)) || Number(value) < 0)) {
                    next.push(`${label} must be a non-negative number.`);
                }
            });
            if (form.errorRateSlo !== '') {
                const errorRateSlo = Number(form.errorRateSlo);
                if (!Number.isFinite(errorRateSlo) || errorRateSlo < 0 || errorRateSlo > 100) {
                    next.push('Error Rate SLO must be a percentage from 0 to 100.');
                }
            }
        }
        if (step === 3 && !form.runName.trim()) next.push('Task name is required.');
        return next;
    };

    const validateStep = (step) => {
        const next = validationErrorsForStep(step);
        setErrors(next);
        return next.length === 0;
    };

    const validate = () => {
        const next = TASK_WIZARD_STEPS.flatMap((_, step) => validationErrorsForStep(step));
        setErrors(next);
        return next.length === 0;
    };

    const generateTaskIdentity = () => {
        const model = form.modelName.trim().split('/').filter(Boolean).pop() || 'model';
        const scenario = scenarioDisplayName(form.scenario);
        const dataset = selectedDataset?.display_name || displayName(form.dataset);
        const endpoint = form.endpointUrl.trim();
        const sla = [
            form.ttftSlo !== '' ? `TTFT SLO ${form.ttftSlo} ms` : '',
            form.tpotSlo !== '' ? `TPOT SLO ${form.tpotSlo} ms` : '',
            form.errorRateSlo !== '' ? `Error Rate SLO ${form.errorRateSlo}%` : '',
        ].filter(Boolean);
        return {
            runName: `${model} · ${scenario} · ${dataset} · ${taskNameSuffix}`,
            description: [
                `Replay the ${dataset} trace for the ${scenario} scenario against ${model} at ${endpoint}.`,
                form.endpointMode === 'deployment'
                    ? `The endpoint is the published Model Service ${selectedModelService?.name || form.endpointModelServiceId}.`
                    : 'The endpoint is external.',
                supportsTraceRange
                    ? `Replay source seconds ${Math.round(Number(form.traceStartSeconds))}–${Math.round(Number(form.traceEndSeconds))} with ${displayName(form.backend)} for ${form.durationSeconds}s at ${form.scaleFactor}x scale.`
                    : `Replay with ${displayName(form.backend)} for ${form.durationSeconds}s at ${form.scaleFactor}x scale.`,
                sla.length ? `${sla.join(' and ')}.` : '',
            ].filter(Boolean).join(' '),
        };
    };

    const advanceWizard = () => {
        if (!validateStep(wizardStep)) return;
        if (wizardStep === 2) {
            setForm((current) => ({ ...current, ...generateTaskIdentity() }));
        }
        setErrors([]);
        setWizardStep((current) => Math.min(current + 1, TASK_WIZARD_STEPS.length - 1));
    };

    const startTask = async () => {
        if (!validate()) return;
        setSubmitting(true);
        setActionError('');
        const backendOptions = {};
        if (form.backend === 'trace-replayer') {
            [
                ['num_producer', form.numProducer],
                ['channel_capacity', form.channelCapacity],
                ['threads', form.threads],
                ['early_stop_error_threshold', form.earlyStopErrorThreshold],
            ].forEach(([key, value]) => {
                if (value !== '') backendOptions[key] = Number(value);
            });
        } else {
            aiperfAdvancedOptions.forEach((option) => {
                const value = form[AIPERF_OPTION_FORM_KEYS[option.name]];
                if (value !== '') backendOptions[option.name] = Number(value);
            });
        }
        if (form.ttftSlo !== '') backendOptions.ttft_slo = Number(form.ttftSlo) / 1000;
        if (form.tpotSlo !== '') backendOptions.tpot_slo = Number(form.tpotSlo) / 1000;
        if (form.errorRateSlo !== '') backendOptions.error_rate_slo = Number(form.errorRateSlo);
        const body = {
            name: form.runName.trim() || `${scenarioDisplayName(form.scenario)} simulation`,
            description: form.description.trim()
                || `${scenarioDisplayName(form.scenario)} scenario using ${form.dataset}. Scenario is a label; dataset selected independently.`,
            scenario: form.scenario,
            backend: form.backend,
            endpoint_mode: form.endpointMode,
            endpoint_namespace: null,
            endpoint_service: null,
            endpoint_deployment_execution_id: null,
            endpoint_cluster_id: null,
            endpoint_cluster_name: null,
            endpoint_deployment_name: null,
            model_service_group_id: form.endpointMode === 'deployment' ? (form.endpointModelServiceId || null) : null,
            endpoint_url: form.endpointUrl.trim(),
            api_key: form.apiKey.trim() || null,
            model_name: form.modelName.trim(),
            trace_dataset: form.dataset,
            trace_path: form.tracePath,
            duration_seconds: Number(form.durationSeconds),
            scale_factor: supportsScaleFactor ? Number(form.scaleFactor) : 1,
            trace_start_seconds: supportsTraceRange ? Number(form.traceStartSeconds) : 0,
            trace_end_seconds: supportsTraceRange ? Number(form.traceEndSeconds) : null,
            trace_timeout_seconds: Number(form.traceTimeoutSeconds),
            fixed_schedule: true,
            stream: true,
            backend_options: backendOptions,
        };
        try {
            const payload = await requestJson('/api/v1/simulation/tasks', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const nextTask = unwrapTask(payload);
            const nextId = payload?.task_id || nextTask?.id || nextTask?.task_id;
            const createdTask = { ...nextTask, id: nextTask?.id || nextId, status: nextTask?.status || payload?.status || 'queued' };
            setTasks((current) => [createdTask, ...current.filter((item) => (item.id || item.task_id) !== nextId)]);
            setIsNewTaskOpen(false);
            setErrors([]);
            await loadTasks({ quiet: true });
            if (nextId) onNavigate?.('optimization-simulate-details', { taskId: nextId });
        } catch (error) {
            setActionError(`Unable to start simulation: ${error.message}`);
        } finally {
            setSubmitting(false);
        }
    };

    const stopTask = async (item) => {
        const id = item?.id || item?.task_id;
        if (!id) return;
        setStoppingTaskId(id);
        setActionError('');
        try {
            const payload = await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(id)}/stop`, {
                method: 'POST',
            });
            setTasks((current) => current.map((item) => (
                (item.id || item.task_id) === id
                    ? { ...item, status: payload?.status || 'cancelling', progress_message: 'Cancellation requested' }
                    : item
            )));
        } catch (error) {
            setActionError(`Unable to stop simulation: ${error.message}`);
        } finally {
            setStoppingTaskId('');
        }
    };

    const deleteSimulationTask = (item) => {
        const id = item?.id || item?.task_id;
        if (!id) return;
        const status = String(item.status || '').toLowerCase();
        if (RUNNING_STATUSES.has(status)) {
            setActionError('Stop the simulation task before deleting it.');
            return;
        }
        setDeleteError('');
        setTaskPendingDelete(item);
    };

    const closeDeleteConfirmation = () => {
        setTaskPendingDelete(null);
        setDeleteError('');
    };

    const confirmDeleteSimulationTask = async () => {
        const id = taskPendingDelete?.id || taskPendingDelete?.task_id;
        if (!id) return;
        setDeletingTaskId(id);
        setDeleteError('');
        try {
            await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' });
            setTasks((current) => current.filter((candidate) => (candidate.id || candidate.task_id) !== id));
            setTaskPendingDelete(null);
            await loadTasks({ quiet: true });
        } catch (error) {
            setDeleteError(`Unable to delete simulation task: ${error.message}`);
        } finally {
            setDeletingTaskId('');
        }
    };

    const rerunSimulationTask = async (item) => {
        const id = item?.id || item?.task_id;
        if (!id || RUNNING_STATUSES.has(String(item.status || '').toLowerCase())) return;
        setRerunningTaskId(id);
        setActionError('');
        try {
            // Legacy deployment tasks created before deployment identity was persisted
            // carry no deployment reference, so a rerun would replay their stale endpoint
            // URL. Fall back to the currently selected Model Service so the backend can
            // re-resolve a fresh, health-probed endpoint.
            let rerunBody = null;
            if (String(item?.endpoint_mode || '').toLowerCase() === 'deployment'
                && !item?.endpoint_deployment_execution_id
                && !item?.endpoint_deployment_run_id
                && !item?.endpoint_deployment_case_id
                && form.endpointModelServiceId) {
                rerunBody = {
                    model_service_group_id: form.endpointModelServiceId,
                };
            }
            const payload = await requestJson(`/api/v1/simulation/tasks/${encodeURIComponent(id)}/rerun`, {
                method: 'POST',
                headers: rerunBody ? { 'Content-Type': 'application/json' } : undefined,
                body: rerunBody ? JSON.stringify(rerunBody) : undefined,
            });
            const rerun = unwrapTask(payload);
            const rerunId = rerun?.id || payload?.task_id;
            const nextTask = { ...rerun, id: rerun?.id || rerunId, status: rerun?.status || 'queued' };
            setTasks((current) => [nextTask, ...current.filter((candidate) => (
                (candidate.id || candidate.task_id) !== rerunId
            ))]);
            await loadTasks({ quiet: true });
            if (rerunId) onNavigate?.('optimization-simulate-details', { taskId: rerunId });
        } catch (error) {
            setActionError(`Unable to rerun simulation: ${error.message}`);
        } finally {
            setRerunningTaskId('');
        }
    };

    const refreshAll = async () => {
        setActionError('');
        await Promise.all([
            loadCatalogs(),
            loadTasks(),
        ]);
    };

    const openNewTask = () => {
        setErrors([]);
        setActionError('');
        setDatasetDownloadError('');
        setWizardStep(0);
        setTaskNameSuffix(createShortId());
        setForm((current) => ({ ...current, runName: '', description: '' }));
        setIsNewTaskOpen(true);
    };

    const activeAdvancedFilterCount = Object.values(advancedFilters).filter((value) => value.trim()).length;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const pageStart = total === 0 ? 0 : page * pageSize + 1;
    const pageEnd = Math.min(total, (page + 1) * pageSize);
    const advancedDateInvalid = Boolean(
        draftAdvancedFilters.createdAfter
        && draftAdvancedFilters.createdBefore
        && draftAdvancedFilters.createdAfter > draftAdvancedFilters.createdBefore
    );
    const openAdvancedFilters = () => {
        setDraftAdvancedFilters({ ...advancedFilters });
        setIsAdvancedOpen(true);
    };
    const clearAllFilters = () => {
        setPage(0);
        setStatusFilter('');
        setSearchInput('');
        setSearch('');
        setAdvancedFilters({ ...EMPTY_ADVANCED_FILTERS });
        setDraftAdvancedFilters({ ...EMPTY_ADVANCED_FILTERS });
    };

    return (
        <ModulePage className={embedded ? 'min-h-0' : undefined} contentClassName={`space-y-5 ${embedded ? 'py-0' : ''}`}>
                {!embedded && (
                    <ModuleHeader
                        icon={SimulationIcon}
                        title="Simulation"
                        description="Replay production traces against an inference endpoint."
                        onToggleMobileNav={onToggleMobileNav}
                        actions={
                            <div className="flex items-center gap-2">
                                <Button variant="outline" size="icon" onClick={refreshAll} disabled={catalogLoading || listLoading} title="Refresh" aria-label="Refresh simulation data">
                                    <RefreshCw className={`h-4 w-4 ${catalogLoading || listLoading ? 'animate-spin' : ''}`} />
                                </Button>
                                <Button variant="sky" size="sm" onClick={openNewTask}>
                                    <Plus className="h-3.5 w-3.5" /> New Task
                                </Button>
                            </div>
                        }
                    />
                )}
                {embedded && (
                    <div className="flex justify-end gap-2">
                        <Button variant="outline" size="icon" onClick={refreshAll} disabled={catalogLoading || listLoading} title="Refresh" aria-label="Refresh simulation data">
                            <RefreshCw className={`h-4 w-4 ${catalogLoading || listLoading ? 'animate-spin' : ''}`} />
                        </Button>
                        <Button variant="sky" size="sm" onClick={openNewTask}>
                            <Plus className="h-3.5 w-3.5" /> New Task
                        </Button>
                    </div>
                )}
                {catalogError && (
                    <div role="status" className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                        <span>Some simulation catalogs could not be loaded. {catalogError}</span>
                    </div>
                )}
                {actionError && (
                    <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {actionError}
                    </div>
                )}

                <section aria-label="Simulation task summary" className="flex flex-col gap-3 rounded-xl border border-slate-900/80 bg-slate-900/40 px-3 py-2 lg:flex-row">
                    <button
                        type="button"
                        onClick={() => {
                            setPage(0);
                            setStatusFilter('');
                        }}
                        className={`flex min-w-44 items-center justify-between rounded-lg border px-4 py-2 text-left transition-all duration-300 ${
                            statusFilter === ''
                                ? 'border-cyan-500/40 bg-slate-900 shadow-[0_0_12px_rgba(6,182,212,0.12)]'
                                : 'border-slate-800/60 bg-slate-900/40 hover:border-slate-700/60 hover:bg-slate-800/40'
                        }`}
                    >
                        <div>
                            <div className="text-[10px] font-bold uppercase leading-none tracking-wider text-slate-400/90">Simulation Tasks</div>
                            <div className="mt-1.5 text-[9px] leading-none text-slate-500">All recorded runs</div>
                        </div>
                        <span className={`text-xl font-black ${statusFilter === '' ? 'text-cyan-400' : 'text-slate-300'}`}>{formatNumber(total)}</span>
                    </button>

                    <div className="flex min-w-0 flex-1 flex-col justify-between rounded-xl border border-slate-900/80 bg-slate-900/40 px-3 py-2">
                        <div className="pb-2 pt-0.5 text-[10px] font-bold uppercase leading-none tracking-wider text-slate-400/90">
                            Simulation Pipeline — Task Status Tracking
                        </div>
                        <div className="flex items-center justify-between gap-1 rounded-lg border border-slate-900/60 p-1 select-none">
                            {[
                                {
                                    key: 'completed', label: 'Completed', value: counts.completed,
                                    active: '-translate-y-0.5 border-emerald-500/35 bg-emerald-500/5 shadow-[0_0_12px_rgba(16,185,129,0.08)]',
                                    bar: 'bg-emerald-500/55', number: 'text-emerald-400',
                                },
                                {
                                    key: 'running', label: 'Running', value: counts.running,
                                    active: '-translate-y-0.5 border-blue-500/35 bg-blue-500/5 shadow-[0_0_12px_rgba(59,130,246,0.08)]',
                                    bar: 'bg-blue-500/55', number: 'text-blue-400',
                                },
                                {
                                    key: 'queued', label: 'Queued', value: counts.queued,
                                    active: '-translate-y-0.5 border-amber-500/35 bg-amber-500/5 shadow-[0_0_12px_rgba(245,158,11,0.08)]',
                                    bar: 'bg-amber-500/55', number: 'text-amber-400',
                                },
                            ].map((stage, index) => (
                                <React.Fragment key={stage.key}>
                                    {index > 0 && <ArrowRight className="h-3 w-3 shrink-0 text-slate-800" />}
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setPage(0);
                                            setStatusFilter(statusFilter === stage.key ? '' : stage.key);
                                        }}
                                        className={`relative flex flex-1 items-center overflow-hidden rounded-md border py-1 pl-3 pr-2 transition-all duration-300 ${
                                            statusFilter === stage.key
                                                ? stage.active
                                                : 'border-transparent bg-slate-900/25 hover:-translate-y-0.5 hover:border-slate-800/60 hover:bg-slate-900/40'
                                        }`}
                                    >
                                        <span className={`absolute bottom-1 left-0 top-1 w-0.5 rounded-r ${stage.bar}`} />
                                        <span className="flex flex-col items-start text-left leading-none">
                                            <span className="text-[8px] font-bold uppercase tracking-wider text-slate-400/90">{stage.label}</span>
                                            <span className={`mt-0.5 text-sm font-extrabold ${statusFilter === stage.key ? stage.number : 'text-slate-200'}`}>{formatNumber(stage.value)}</span>
                                        </span>
                                    </button>
                                </React.Fragment>
                            ))}

                            <div className="mx-1 h-5 w-px shrink-0 self-center bg-slate-900" />

                            {[
                                {
                                    key: 'failed', label: 'Failed', value: counts.failed,
                                    active: '-translate-y-0.5 border-red-500/35 bg-red-500/5',
                                    bar: 'bg-red-500/55', number: 'text-red-400',
                                },
                                {
                                    key: 'cancelled', label: 'Cancelled', value: counts.cancelled,
                                    active: '-translate-y-0.5 border-slate-500/35 bg-slate-500/5',
                                    bar: 'bg-slate-500/55', number: 'text-slate-300',
                                },
                            ].map((stage) => (
                                <button
                                    key={stage.key}
                                    type="button"
                                    onClick={() => {
                                        setPage(0);
                                        setStatusFilter(statusFilter === stage.key ? '' : stage.key);
                                    }}
                                    className={`relative flex flex-1 items-center overflow-hidden rounded-md border py-1 pl-3 pr-2 transition-all duration-300 ${
                                        statusFilter === stage.key
                                            ? stage.active
                                            : 'border-transparent bg-slate-900/25 hover:-translate-y-0.5 hover:border-slate-800/60 hover:bg-slate-900/40'
                                    }`}
                                >
                                    <span className={`absolute bottom-1 left-0 top-1 w-0.5 rounded-r ${stage.bar}`} />
                                    <span className="flex flex-col items-start text-left leading-none">
                                        <span className="text-[8px] font-bold uppercase tracking-wider text-slate-400/90">{stage.label}</span>
                                        <span className={`mt-0.5 text-sm font-extrabold ${statusFilter === stage.key ? stage.number : 'text-slate-200'}`}>{formatNumber(stage.value)}</span>
                                    </span>
                                </button>
                            ))}
                        </div>
                    </div>
                </section>

                <section
                    aria-labelledby="simulation-tasks-heading"
                    className="relative z-10 flex flex-col gap-3.5 rounded-3xl border border-slate-900/90 bg-[#070b13]/65 p-5 shadow-2xl backdrop-blur-md"
                >
                    <div className="relative z-20 flex flex-wrap items-center gap-3 rounded-2xl border border-slate-800/60 bg-[#0a0f1d] p-3 shadow-md">
                        <div className="min-w-[240px] flex-1">
                            <div className="relative">
                                <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                                <Input
                                    id="simulation-task-search"
                                    className="rounded-xl pl-9 pr-4 text-xs font-medium"
                                    type="search"
                                    placeholder="Search task name, scenario, backend, or dataset..."
                                    value={searchInput}
                                    onChange={(event) => setSearchInput(event.target.value)}
                                />
                            </div>
                        </div>
                        <div id="simulation-model-filter" className="w-48 shrink-0">
                            <MultiSelectDropdown
                                label="Model"
                                options={facets.models}
                                selected={advancedFilters.model ? new Set([advancedFilters.model]) : new Set()}
                                onChange={(value) => {
                                    setPage(0);
                                    setAdvancedFilters((current) => ({
                                        ...current,
                                        model: value === current.model ? '' : value,
                                    }));
                                }}
                                formatLabel={(value) => value}
                            />
                        </div>
                        <div id="simulation-dataset-filter" className="w-48 shrink-0 border-r border-slate-800/40 pr-3">
                            <MultiSelectDropdown
                                label="Dataset"
                                options={facets.datasets}
                                selected={advancedFilters.dataset ? new Set([advancedFilters.dataset]) : new Set()}
                                onChange={(value) => {
                                    setPage(0);
                                    setAdvancedFilters((current) => ({
                                        ...current,
                                        dataset: value === current.dataset ? '' : value,
                                    }));
                                }}
                                formatLabel={(value) => value}
                            />
                        </div>
                        <Button variant="secondary" size="sm" onClick={openAdvancedFilters}>
                            <SlidersHorizontal className="h-3.5 w-3.5" />
                            Advanced Filters{activeAdvancedFilterCount > 0 ? ` (${activeAdvancedFilterCount})` : ''}
                        </Button>
                    </div>

                    <div className="flex min-h-[52px] flex-col items-start justify-between gap-3 border-b border-slate-800/60 px-4 py-2.5 sm:flex-row sm:items-center">
                        <div className="flex items-center gap-2">
                            <Server className="h-[15px] w-[15px] text-cyan-400" />
                            <h2 id="simulation-tasks-heading" className="text-sm font-bold text-slate-300">
                                {formatNumber(total)} Matching Task{total === 1 ? '' : 's'}
                            </h2>
                        </div>
                        <div className="flex items-center gap-2">
                            <Button variant="secondary" size="sm" onClick={() => loadTasks()}>
                                <RefreshCw className="h-3 w-3" /> Refresh
                            </Button>
                            {(statusFilter || searchInput || activeAdvancedFilterCount > 0) && (
                                <Button
                                    variant="secondary"
                                    size="sm"
                                    onClick={clearAllFilters}
                                >
                                    Clear Filters
                                </Button>
                            )}
                        </div>
                    </div>
                    {listLoading ? (
                        <div className="flex items-center justify-center gap-2 rounded-2xl border border-slate-800 bg-slate-900/80 py-12 text-sm text-slate-400">
                            <Spinner /> Loading tasks…
                        </div>
                    ) : tasks.length === 0 ? (
                        <div className="rounded-2xl border border-slate-800 bg-slate-900/80">
                            <EmptyState
                                icon={<Activity className="h-8 w-8" />}
                                title="No simulation tasks found"
                                message={statusFilter || search || activeAdvancedFilterCount > 0 ? 'Adjust the filters or create a new task.' : 'Create a task to replay a production trace.'}
                                action={<Button variant="sky" onClick={openNewTask}><Plus className="h-3.5 w-3.5" /> New Task</Button>}
                            />
                        </div>
                    ) : (
                        <div className="flex flex-col gap-2">
                            {tasks.map((item) => {
                                const id = item.id || item.task_id;
                                const status = String(item.status || '').toLowerCase();
                                const metrics = taskMetrics(item);
                                const itemProgress = Math.max(0, Math.min(100, Number(item.progress_percent ?? item.progress ?? 0)));
                                const itemTimeline = Array.isArray(item?.live_summary?.completion_timeline)
                                    ? item.live_summary.completion_timeline.map((point) => ({
                                        ...point,
                                        time_seconds: point.end_seconds,
                                    }))
                                    : [];
                                return (
                                    <article
                                        key={id}
                                        role="button"
                                        tabIndex={0}
                                        onClick={() => openTaskDetails(item)}
                                        onKeyDown={(event) => {
                                            if (event.key === 'Enter' || event.key === ' ') {
                                                event.preventDefault();
                                                openTaskDetails(item);
                                            }
                                        }}
                                        className={`relative cursor-pointer overflow-hidden rounded-lg border p-4 shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/50 ${
                                            'border-slate-800 bg-slate-900 hover:border-slate-700 hover:bg-slate-800/50'
                                        }`}
                                    >
                                        {status === 'running' && itemTimeline.length > 0 && (
                                            <div
                                                aria-hidden="true"
                                                className="pointer-events-none absolute inset-y-0 right-0 z-0 w-3/4 opacity-20 [mask-image:linear-gradient(to_right,transparent,black_28%)]"
                                            >
                                                <ResponsiveContainer width="100%" height="100%">
                                                    <AreaChart data={itemTimeline} margin={{ top: 8, right: 0, left: 0, bottom: 0 }}>
                                                        <Area
                                                            type="monotone"
                                                            dataKey="arrived_requests"
                                                            stroke="#60a5fa"
                                                            strokeWidth={1.5}
                                                            fill="#60a5fa"
                                                            fillOpacity={0.2}
                                                            dot={false}
                                                            isAnimationActive={false}
                                                        />
                                                        <Area
                                                            type="monotone"
                                                            dataKey="successful_requests"
                                                            stroke="#34d399"
                                                            strokeWidth={1.5}
                                                            fill="#34d399"
                                                            fillOpacity={0.2}
                                                            dot={false}
                                                            isAnimationActive={false}
                                                        />
                                                        <Area
                                                            type="monotone"
                                                            dataKey="failed_requests"
                                                            stroke="#fb7185"
                                                            strokeWidth={1.5}
                                                            fill="#fb7185"
                                                            fillOpacity={0.2}
                                                            dot={false}
                                                            isAnimationActive={false}
                                                        />
                                                    </AreaChart>
                                                </ResponsiveContainer>
                                            </div>
                                        )}
                                        <div className="relative z-10 flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                                            <div className="min-w-0 flex-1">
                                                <div className="flex flex-wrap items-center gap-2">
                                                    <h3 className="truncate text-sm font-semibold text-slate-100">{item.name || 'Simulation task'}</h3>
                                                    <StatusChip
                                                        status={taskStatusChip(status)}
                                                        label={displayName(status)}
                                                        pulse={status === 'running'}
                                                    />
                                                </div>
                                                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
                                                    <span><span className="text-slate-600">Scenario:</span> {scenarioDisplayName(item.scenario) || '—'}</span>
                                                    <span><span className="text-slate-600">Model:</span> {item.model_name || '—'}</span>
                                                    <span><span className="text-slate-600">Backend:</span> {displayName(taskBackend(item))}</span>
                                                    <span className="max-w-full truncate"><span className="text-slate-600">Dataset:</span> {taskDataset(item)}</span>
                                                    <span><span className="text-slate-600">Created:</span> {item.created_at ? new Date(item.created_at).toLocaleString() : '—'}</span>
                                                </div>
                                                {RUNNING_STATUSES.has(status) && (
                                                    <div className="mt-3 max-w-xl">
                                                        <div className="mb-1 flex items-center justify-between gap-3 text-[10px] text-slate-400">
                                                            <span className="truncate">{item.progress_message || displayName(status)}</span>
                                                            <span className="shrink-0 font-mono text-cyan-300">{formatNumber(itemProgress, '%')}</span>
                                                        </div>
                                                        <div className="h-1.5 overflow-hidden rounded-full bg-slate-800" role="progressbar" aria-valuenow={itemProgress} aria-valuemin="0" aria-valuemax="100">
                                                            <div className="h-full rounded-full bg-gradient-to-r from-emerald-500 to-cyan-400 transition-all duration-700" style={{ width: `${itemProgress}%` }} />
                                                        </div>
                                                    </div>
                                                )}
                                            </div>
                                            <div className="grid grid-cols-2 gap-x-5 gap-y-2 text-xs sm:grid-cols-4">
                                                <div><div className="text-slate-600">Req/s</div><div className="font-semibold text-slate-300">{formatNumber(metrics.requestRate)}</div></div>
                                                <div>
                                                    <div className="text-slate-600">TTFT</div>
                                                    <div className={`font-semibold ${metrics.ttft === null || metrics.ttftSloMs === null ? 'text-slate-300' : metrics.ttft > metrics.ttftSloMs ? 'text-rose-300' : 'text-emerald-300'}`}>
                                                        {formatMilliseconds(metrics.ttft)}
                                                    </div>
                                                </div>
                                                <div>
                                                    <div className="text-slate-600">TPOT</div>
                                                    <div className={`font-semibold ${metrics.tpot === null || metrics.tpotSloMs === null ? 'text-slate-300' : metrics.tpot > metrics.tpotSloMs ? 'text-rose-300' : 'text-emerald-300'}`}>
                                                        {formatMilliseconds(metrics.tpot)}
                                                    </div>
                                                </div>
                                                <div>
                                                    <div className="text-slate-600">Error Rate</div>
                                                    <div className={`font-semibold ${
                                                        metrics.errorRate === null
                                                            ? 'text-slate-300'
                                                            : metrics.errorRateSlo === null
                                                                ? metrics.errorRate > 0 ? 'text-rose-300' : 'text-emerald-300'
                                                                : metrics.errorRate > metrics.errorRateSlo ? 'text-rose-300' : 'text-emerald-300'
                                                    }`}>
                                                        {formatNumber(metrics.errorRate, '%')}
                                                    </div>
                                                </div>
                                            </div>
                                            <div className="flex items-center gap-2">
                                                {RUNNING_STATUSES.has(status) ? (
                                                    <Button
                                                        variant="dangerOutline"
                                                        size="sm"
                                                        onClick={(event) => {
                                                            event.stopPropagation();
                                                            stopTask(item);
                                                        }}
                                                        onKeyDown={(event) => event.stopPropagation()}
                                                        disabled={!STOPPABLE_STATUSES.has(status)}
                                                        isLoading={stoppingTaskId === id}
                                                    >
                                                        <Square className="h-3.5 w-3.5" /> Stop
                                                    </Button>
                                                ) : (
                                                    <Button
                                                        variant="secondary"
                                                        size="sm"
                                                        onClick={(event) => {
                                                            event.stopPropagation();
                                                            rerunSimulationTask(item);
                                                        }}
                                                        onKeyDown={(event) => event.stopPropagation()}
                                                        isLoading={rerunningTaskId === id}
                                                    >
                                                        <RefreshCw className="h-3.5 w-3.5" /> Rerun
                                                    </Button>
                                                )}
                                                {!RUNNING_STATUSES.has(status) && (
                                                    <Button
                                                        variant="dangerOutline"
                                                        size="sm"
                                                        onClick={(event) => {
                                                            event.stopPropagation();
                                                            deleteSimulationTask(item);
                                                        }}
                                                        onKeyDown={(event) => event.stopPropagation()}
                                                        isLoading={deletingTaskId === id}
                                                        title="Delete task and all backend data"
                                                    >
                                                        <Trash2 className="h-3.5 w-3.5" /> Delete
                                                    </Button>
                                                )}
                                            </div>
                                        </div>
                                    </article>
                                );
                            })}
                            <PaginationControls className="mt-2" page={page} totalPages={totalPages} onPageChange={setPage}
                                total={total} pageStart={pageStart} pageEnd={pageEnd} itemLabel="tasks" formatValue={formatNumber}
                                pageSize={pageSize} pageSizeOptions={[5, 7, 10, 20, 50, 100]} onPageSizeChange={setPageSize}
                                pageSizeId="simulation-page-size" />
                        </div>
                    )}
                </section>

                {typeof document !== 'undefined' && isAdvancedOpen && createPortal(
                    <div
                        className="fixed inset-0 z-[55] cursor-pointer bg-black/40 backdrop-blur-[1.5px]"
                        onClick={() => setIsAdvancedOpen(false)}
                    />,
                    document.body,
                )}
                {typeof document !== 'undefined' && createPortal(
                    <aside
                        aria-label="Advanced simulation task filters"
                        className={`fixed right-4 top-20 z-[60] flex h-[calc(100vh-6rem)] w-[420px] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-3xl border border-slate-900 bg-slate-950/95 shadow-2xl backdrop-blur-xl transition-transform duration-300 ${
                            isAdvancedOpen ? 'translate-x-0' : 'translate-x-[calc(100%+2rem)]'
                        }`}
                    >
                        <div className="flex items-center justify-between border-b border-slate-900/60 bg-slate-950/40 p-4">
                            <div className="flex items-center gap-2">
                                <SlidersHorizontal className="h-4 w-4 text-cyan-400" />
                                <span className="text-sm font-bold tracking-wide text-white">Advanced Filters</span>
                            </div>
                            <button
                                type="button"
                                aria-label="Close advanced filters"
                                onClick={() => setIsAdvancedOpen(false)}
                                className="cursor-pointer rounded-lg p-1 text-slate-500 transition-all hover:bg-slate-900 hover:text-white"
                            >
                                <X className="h-4 w-4" />
                            </button>
                        </div>

                        <div className="custom-scrollbar flex-1 space-y-6 overflow-y-auto p-5">
                            <section className="space-y-3.5 rounded-xl border border-slate-900/60 bg-slate-950/20 p-4">
                                <h3 className="border-b border-slate-800/60 pb-1.5 text-[10px] font-black uppercase tracking-wider text-cyan-400">Execution</h3>
                                <div>
                                    <Label htmlFor="simulation-filter-backend">Backend</Label>
                                    <Select
                                        id="simulation-filter-backend"
                                        value={draftAdvancedFilters.backend}
                                        onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, backend: event.target.value }))}
                                    >
                                        <option value="">Any backend</option>
                                        {facets.backends.map((value) => <option key={value} value={value}>{displayName(value)}</option>)}
                                    </Select>
                                </div>
                                <div>
                                    <Label htmlFor="simulation-filter-model">Model</Label>
                                    <Select
                                        id="simulation-filter-model"
                                        value={draftAdvancedFilters.model}
                                        onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, model: event.target.value }))}
                                    >
                                        <option value="">Any model</option>
                                        {facets.models.map((value) => <option key={value} value={value}>{value}</option>)}
                                    </Select>
                                </div>
                            </section>

                            <section className="space-y-3.5 rounded-xl border border-slate-900/60 bg-slate-950/20 p-4">
                                <h3 className="border-b border-slate-800/60 pb-1.5 text-[10px] font-black uppercase tracking-wider text-cyan-400">Workload</h3>
                                <div>
                                    <Label htmlFor="simulation-filter-dataset">Dataset</Label>
                                    <Select
                                        id="simulation-filter-dataset"
                                        value={draftAdvancedFilters.dataset}
                                        onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, dataset: event.target.value }))}
                                    >
                                        <option value="">Any dataset</option>
                                        {facets.datasets.map((value) => <option key={value} value={value}>{displayName(value)}</option>)}
                                    </Select>
                                </div>
                                <div>
                                    <Label htmlFor="simulation-filter-trace-format">Trace format</Label>
                                    <Select
                                        id="simulation-filter-trace-format"
                                        value={draftAdvancedFilters.traceFormat}
                                        onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, traceFormat: event.target.value }))}
                                    >
                                        <option value="">Any trace format</option>
                                        {facets.trace_formats.map((value) => <option key={value} value={value}>{displayName(value)}</option>)}
                                    </Select>
                                </div>
                            </section>

                            <section className="space-y-3.5 rounded-xl border border-slate-900/60 bg-slate-950/20 p-4">
                                <h3 className="border-b border-slate-800/60 pb-1.5 text-[10px] font-black uppercase tracking-wider text-cyan-400">Target</h3>
                                <div>
                                    <Label htmlFor="simulation-filter-endpoint">Endpoint contains</Label>
                                    <Input
                                        id="simulation-filter-endpoint"
                                        list="simulation-endpoint-options"
                                        placeholder="Host or URL"
                                        value={draftAdvancedFilters.endpoint}
                                        onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, endpoint: event.target.value }))}
                                    />
                                    <datalist id="simulation-endpoint-options">
                                        {facets.endpoints.map((value) => <option key={value} value={value} />)}
                                    </datalist>
                                </div>
                            </section>

                            <section className="space-y-3.5 rounded-xl border border-slate-900/60 bg-slate-950/20 p-4">
                                <h3 className="border-b border-slate-800/60 pb-1.5 text-[10px] font-black uppercase tracking-wider text-cyan-400">Created Time</h3>
                                <div className="grid grid-cols-2 gap-3">
                                    <div>
                                        <Label htmlFor="simulation-filter-created-after">From</Label>
                                        <Input
                                            id="simulation-filter-created-after"
                                            type="date"
                                            value={draftAdvancedFilters.createdAfter}
                                            onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, createdAfter: event.target.value }))}
                                        />
                                    </div>
                                    <div>
                                        <Label htmlFor="simulation-filter-created-before">To</Label>
                                        <Input
                                            id="simulation-filter-created-before"
                                            type="date"
                                            value={draftAdvancedFilters.createdBefore}
                                            onChange={(event) => setDraftAdvancedFilters((current) => ({ ...current, createdBefore: event.target.value }))}
                                        />
                                    </div>
                                </div>
                                {advancedDateInvalid && <p className="text-xs text-red-400">The From date must not be later than the To date.</p>}
                            </section>
                        </div>

                        <div className="flex items-center justify-between border-t border-slate-900/60 bg-slate-950/60 p-4">
                            <Button
                                variant="secondary"
                                size="sm"
                                onClick={() => setDraftAdvancedFilters({ ...EMPTY_ADVANCED_FILTERS })}
                            >
                                Clear All
                            </Button>
                            <div className="flex gap-2">
                                <Button variant="secondary" size="sm" onClick={() => setIsAdvancedOpen(false)}>Cancel</Button>
                                <Button
                                    variant="sky"
                                    size="sm"
                                    disabled={advancedDateInvalid}
                                    onClick={() => {
                                        setPage(0);
                                        setAdvancedFilters({ ...draftAdvancedFilters });
                                        setIsAdvancedOpen(false);
                                    }}
                                >
                                    Apply Filters
                                </Button>
                            </div>
                        </div>
                    </aside>,
                    document.body,
                )}

                <div className="space-y-6">
                    <Modal
                        isOpen={Boolean(taskPendingDelete)}
                        onClose={deletingTaskId ? undefined : closeDeleteConfirmation}
                        closeOnBackdrop={!deletingTaskId}
                        closeOnEscape={!deletingTaskId}
                        size="md"
                        title={(
                            <span className="flex items-center gap-3">
                                <span className="rounded-lg bg-rose-500/15 p-2 text-rose-400">
                                    <Trash2 className="h-5 w-5" />
                                </span>
                                Delete Simulation Task
                            </span>
                        )}
                        subtitle="This action cannot be undone."
                        footer={(
                            <>
                                <Button variant="secondary" onClick={closeDeleteConfirmation} disabled={Boolean(deletingTaskId)}>
                                    Cancel
                                </Button>
                                <Button variant="dangerOutline" onClick={confirmDeleteSimulationTask} isLoading={Boolean(deletingTaskId)}>
                                    <Trash2 className="h-3.5 w-3.5" /> Delete Task
                                </Button>
                            </>
                        )}
                    >
                        <div className="space-y-4">
                            <div className="rounded-xl border border-rose-500/25 bg-rose-500/10 p-4">
                                <div className="flex items-start gap-3">
                                    <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-rose-400" />
                                    <div>
                                        <p className="text-sm font-semibold text-rose-100">
                                            Delete “{taskPendingDelete?.name || taskPendingDelete?.id || taskPendingDelete?.task_id}”?
                                        </p>
                                        <p className="mt-1 text-xs leading-relaxed text-rose-200/70">
                                            The task record, results, logs, and all backend artifacts will be permanently removed.
                                        </p>
                                    </div>
                                </div>
                            </div>
                            {deleteError && (
                                <p role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                                    {deleteError}
                                </p>
                            )}
                        </div>
                    </Modal>

                    <Modal
                        isOpen={isNewTaskOpen}
                        onClose={submitting ? undefined : () => setIsNewTaskOpen(false)}
                        title="New Simulation Task"
                        subtitle={`Step ${wizardStep + 1} of ${TASK_WIZARD_STEPS.length} · ${TASK_WIZARD_STEPS[wizardStep]}`}
                        size="xl"
                        closeOnBackdrop={!submitting}
                        closeOnEscape={!submitting}
                        footer={(
                            <>
                                <Button variant="outline" onClick={() => setIsNewTaskOpen(false)} disabled={submitting}>Cancel</Button>
                                {wizardStep > 0 && (
                                    <Button
                                        variant="outline"
                                        disabled={submitting}
                                        onClick={() => {
                                            setErrors([]);
                                            setWizardStep((current) => current - 1);
                                        }}
                                    >
                                        Back
                                    </Button>
                                )}
                                {wizardStep < TASK_WIZARD_STEPS.length - 1 ? (
                                    <Button variant="sky" onClick={advanceWizard} disabled={downloading}>
                                        Next <ArrowRight className="h-3.5 w-3.5" />
                                    </Button>
                                ) : (
                                    <Button variant="sky" onClick={startTask} isLoading={submitting}>
                                        <Play className="h-3.5 w-3.5" /> Create Task
                                    </Button>
                                )}
                            </>
                        )}
                    >
                    <div className="mx-auto max-w-4xl space-y-6">
                        <ol className="grid grid-cols-4 gap-2" aria-label="Task creation progress">
                            {TASK_WIZARD_STEPS.map((label, index) => (
                                <li key={label} className="min-w-0">
                                    <div className={`h-1.5 rounded-full transition-colors ${index <= wizardStep ? 'bg-cyan-400' : 'bg-slate-800'}`} />
                                    <div className={`mt-2 truncate text-[10px] font-bold uppercase tracking-wider ${index === wizardStep ? 'text-cyan-300' : 'text-slate-500'}`}>
                                        {index + 1}. {label}
                                    </div>
                                </li>
                            ))}
                        </ol>
                        {actionError && (
                            <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {actionError}
                            </div>
                        )}
                        {wizardStep === 3 && <section aria-labelledby="basic-information-heading">
                            <h2 id="basic-information-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                                <Info className="h-5 w-5 text-cyan-400" /> Basic Information
                            </h2>
                            <div className="space-y-4">
                                <div>
                                    <Label htmlFor="simulation-run-name">Task name</Label>
                                    <Input id="simulation-run-name" value={form.runName} onChange={(event) => update('runName', event.target.value)} />
                                </div>
                                <div>
                                    <Label htmlFor="simulation-description">Description</Label>
                                    <Textarea
                                        id="simulation-description"
                                        rows={2}
                                        placeholder="Task description..."
                                        value={form.description}
                                        onChange={(event) => update('description', event.target.value)}
                                    />
                                </div>
                                <p className="text-xs text-slate-500">
                                    Data will be saved under <code className="rounded bg-slate-950/70 px-1.5 py-0.5">~/.llm-d-lens/simulations/&#123;task-id&#125;/</code>
                                </p>
                            </div>
                        </section>}

                        {wizardStep === 0 && <section aria-labelledby="benchmark-endpoint-heading">
                            <h2 id="benchmark-endpoint-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                                <Gauge className="h-5 w-5 text-cyan-400" /> Benchmark &amp; Endpoint
                            </h2>
                            <div className="space-y-5">
                                <div className="rounded-lg border border-violet-500/25 bg-violet-500/5 p-4">
                                    <div className="mb-3">
                                        <h3 className="text-sm font-semibold text-violet-200">Benchmark Backend</h3>
                                        <p className="mt-1 text-[11px] text-slate-500">Select the tool that will generate and replay benchmark traffic.</p>
                                    </div>
                                    <Label htmlFor="simulation-backend">Benchmark tool</Label>
                                    <Select
                                        id="simulation-backend"
                                        value={form.backend}
                                        onChange={(event) => chooseBackend(event.target.value)}
                                    >
                                        {backends.map((backend) => {
                                            const value = backend.name || backend.id || backend.value;
                                            return (
                                                <option
                                                    key={value}
                                                    value={value}
                                                    disabled={backend.available === false}
                                                >
                                                    {backend.display_name || backend.label || displayName(value)}
                                                    {backend.available === false ? ' (Unavailable)' : ''}
                                                </option>
                                            );
                                        })}
                                    </Select>
                                    {selectedBackend && (
                                        <p className="mt-1 text-[10px] text-slate-500">
                                            {selectedBackend.version && `Version ${selectedBackend.version} · `}
                                            {selectedBackend.available === false
                                                ? selectedBackend.unavailable_reason || 'Unavailable'
                                                : `${selectedBackend.display_name || displayName(form.backend)} is ready`}
                                        </p>
                                    )}
                                    <details className="mt-4 rounded-lg border border-violet-500/20 bg-slate-950/30">
                                        <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-4 py-3 text-xs font-bold uppercase tracking-wider text-slate-400 hover:text-slate-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/50">
                                            <SlidersHorizontal className="h-4 w-4" /> Advanced backend settings
                                        </summary>
                                        <div className="grid gap-4 border-t border-violet-500/15 p-4 sm:grid-cols-2 lg:grid-cols-3">
                                            {form.backend === 'trace-replayer' ? [
                                                ['numProducer', 'Producers', '1'],
                                                ['channelCapacity', 'Channel capacity', '1'],
                                                ['threads', 'Threads', '1'],
                                                ['earlyStopErrorThreshold', 'Error stop threshold', '0'],
                                            ].map(([key, label, min]) => (
                                                <div key={key}>
                                                    <Label htmlFor={`simulation-${key}`}>{label}</Label>
                                                    <Input id={`simulation-${key}`} type="number" min={min} value={form[key]} onChange={(event) => update(key, event.target.value)} />
                                                </div>
                                            )) : (
                                                <>
                                                    <div>
                                                        <Label htmlFor="simulation-timeout">Prism execution timeout (seconds)</Label>
                                                        <Input id="simulation-timeout" type="number" min="1" value={form.traceTimeoutSeconds} onChange={(event) => update('traceTimeoutSeconds', event.target.value)} />
                                                    </div>
                                                    {aiperfAdvancedOptions.map((option) => {
                                                        const formKey = AIPERF_OPTION_FORM_KEYS[option.name];
                                                        return (
                                                            <div key={option.name}>
                                                                <Label htmlFor={`simulation-${formKey}`}>{option.label}</Label>
                                                                <Input
                                                                    id={`simulation-${formKey}`}
                                                                    type="number"
                                                                    min={option.minimum}
                                                                    max={option.maximum}
                                                                    step="1"
                                                                    placeholder={option.default === undefined ? 'Optional' : undefined}
                                                                    value={form[formKey]}
                                                                    onChange={(event) => update(formKey, event.target.value)}
                                                                />
                                                            </div>
                                                        );
                                                    })}
                                                </>
                                            )}
                                        </div>
                                    </details>
                                </div>

                                <div className="rounded-lg border border-cyan-500/20 bg-cyan-500/5 p-4">
                                <div className="mb-4 flex items-center gap-2">
                                    <Server className="h-4 w-4 text-emerald-400" />
                                    <div>
                                        <h3 className="text-sm font-semibold text-emerald-200">Inference Endpoint</h3>
                                        <p className="mt-0.5 text-[11px] text-slate-500">Configure the model-serving endpoint that receives benchmark requests.</p>
                                    </div>
                                </div>
                                <div className="mb-4 flex flex-wrap gap-5 border-b border-cyan-500/15 pb-4">
                                    <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-200">
                                        <input
                                            type="radio"
                                            name="simulation-endpoint-mode"
                                            value="deployment"
                                            checked={form.endpointMode === 'deployment'}
                                            onChange={(event) => chooseEndpointMode(event.target.value)}
                                            className="accent-emerald-500"
                                        />
                                        <span>Model Service</span>
                                    </label>
                                    <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-200">
                                        <input
                                            type="radio"
                                            name="simulation-endpoint-mode"
                                            value="external"
                                            checked={form.endpointMode === 'external'}
                                            onChange={(event) => chooseEndpointMode(event.target.value)}
                                            className="accent-emerald-500"
                                        />
                                        Endpoint URL
                                    </label>
                                </div>

                                {form.endpointMode === 'deployment' ? (
                                    <div className="mb-4 rounded-lg border border-emerald-500/20 bg-slate-950/35 p-4">
                                        <div className="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
                                            <div>
                                                <Label htmlFor="simulation-endpoint-model-service">Model service</Label>
                                                <Select
                                                    id="simulation-endpoint-model-service"
                                                    value={form.endpointModelServiceId}
                                                    onChange={(event) => selectModelService(event.target.value)}
                                                    disabled={modelServicesLoading || modelServices.length === 0}
                                                >
                                                    <option value="">
                                                        {modelServicesLoading
                                                            ? 'Loading model services…'
                                                            : modelServices.length
                                                                ? 'Choose a model service…'
                                                                : 'No published model service'}
                                                    </option>
                                                    {modelServices.map((item) => (
                                                        <option key={item.id} value={item.id}>
                                                            {item.name}{item.baseModel && item.baseModel !== item.name ? ` · ${item.baseModel}` : ''}{item.clusterName ? ` (${item.clusterName})` : ''}
                                                        </option>
                                                    ))}
                                                </Select>
                                            </div>
                                            <Button
                                                variant="secondary"
                                                onClick={() => void loadModelServices()}
                                                isLoading={modelServicesLoading}
                                            >
                                                <RefreshCw className="h-3.5 w-3.5" /> Refresh
                                            </Button>
                                        </div>
                                        {modelServicesError && (
                                            <p role="alert" className="mt-3 text-xs text-rose-300">{modelServicesError}</p>
                                        )}
                                        {form.endpointModelServiceId && (
                                            <div className="mt-3 rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3 text-xs">
                                                <div className="font-semibold text-emerald-300">Model service ready</div>
                                                <div className="mt-1 font-mono text-slate-400">{selectedModelService?.name || form.endpointModelServiceId}</div>
                                            </div>
                                        )}
                                    </div>
                                ) : (
                                    <div className="mb-4">
                                        <Label htmlFor="simulation-endpoint">Endpoint URL</Label>
                                        <Input id="simulation-endpoint" type="url" placeholder="http://inference.example:8000" value={form.endpointUrl} onChange={(event) => updateExternalEndpoint(event.target.value)} />
                                    </div>
                                )}

                                <div className="mb-4">
                                    <Label htmlFor="simulation-api-key">
                                        Model access token{form.endpointMode === 'deployment' ? '' : ' (optional)'}
                                    </Label>
                                    <Input
                                        id="simulation-api-key"
                                        type="password"
                                        autoComplete="off"
                                        placeholder="lens-mk-…"
                                        value={form.apiKey}
                                        onChange={(event) => update('apiKey', event.target.value)}
                                    />
                                    <p className="mt-1 text-[10px] text-slate-500">
                                        Needed when the harness calls a Model Service through the cluster&apos;s shared Gateway. Used for this run only; never stored.
                                    </p>
                                </div>

                                <div>
                                        <Label htmlFor="simulation-model">Model</Label>
                                        {modelsLoading ? (
                                            <div className="flex min-h-10 items-center gap-2 rounded-lg border border-slate-700 bg-slate-900 px-3 text-xs text-slate-400">
                                                <Spinner /> Loading models from /v1/models…
                                            </div>
                                        ) : endpointModels.length > 1 ? (
                                            <Select id="simulation-model" value={form.modelName} onChange={(event) => update('modelName', event.target.value)}>
                                                <option value="">Choose a model…</option>
                                                {endpointModels.map((model) => <option key={model} value={model}>{model}</option>)}
                                            </Select>
                                        ) : endpointModels.length === 1 ? (
                                            <Input id="simulation-model" value={form.modelName} readOnly />
                                        ) : (
                                            <Input id="simulation-model" placeholder="meta-llama/Llama-3.1-8B-Instruct" value={form.modelName} onChange={(event) => update('modelName', event.target.value)} />
                                        )}
                                        {endpointModels.length === 1 && (
                                            <p className="mt-1 text-[10px] text-emerald-400">Automatically selected from the endpoint.</p>
                                        )}
                                        {modelDiscoveryError && (
                                            <p className="mt-1 text-[10px] text-amber-300">{modelDiscoveryError}</p>
                                        )}
                                </div>
                            </div>
                            </div>
                        </section>}

                        {wizardStep === 1 && <section aria-labelledby="scenario-configuration-heading">
                            <h2 id="scenario-configuration-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                                <MessageSquareText className="h-5 w-5 text-cyan-400" /> Scenario
                            </h2>
                            <div className="rounded-lg border border-cyan-500/20 bg-cyan-500/5 p-4">
                                <fieldset>
                                    <legend className="mb-2 text-xs font-semibold text-slate-400">
                                        Scenarios supported by {selectedBackend?.display_name || displayName(form.backend)}
                                    </legend>
                                    <ToggleGroup
                                        options={availableScenarios}
                                        value={form.scenario}
                                        onChange={chooseScenario}
                                        fullWidth
                                    />
                                </fieldset>
                                <p className="mt-3 text-xs text-slate-500">
                                    The backend defines the scenario abstraction. Selecting a scenario determines which trace datasets are available.
                                </p>
                            </div>
                        </section>}

                        {wizardStep === 1 && <section aria-labelledby="workload-configuration-heading">
                            <h2 id="workload-configuration-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                                <MessageSquareText className="h-5 w-5 text-violet-400" /> Workload
                            </h2>
                            <div className="rounded-lg border border-violet-500/20 bg-violet-500/5 p-4">
                                <label className="mb-4 flex items-center gap-2 text-sm font-medium text-slate-200">
                                    <input type="radio" checked readOnly className="accent-violet-500" />
                                    Trace replay
                                </label>
                                {datasetDownloadError && (
                                    <div role="alert" className="mb-4 flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                                        <span>{datasetDownloadError}</span>
                                    </div>
                                )}
                                {catalogLoading ? (
                                    <div className="flex items-center gap-2 py-6 text-sm text-slate-400"><Spinner /> Loading datasets…</div>
                                ) : !form.scenario ? (
                                    <EmptyState
                                        icon={<MessageSquareText className="h-8 w-8" />}
                                        title="Choose a scenario first"
                                        message="The selected backend and scenario determine the available trace datasets."
                                    />
                                ) : compatibleDatasets.length ? (
                                    <div className="space-y-4">
                                        <div>
                                            <Label htmlFor="simulation-dataset">Trace dataset</Label>
                                            <Select id="simulation-dataset" value={form.dataset} onChange={(event) => chooseDataset(event.target.value)}>
                                                <option value="">Choose a dataset…</option>
                                                {compatibleDatasets.map((item) => {
                                                    const value = item.name || item.id || item.value;
                                                    return <option key={value} value={value}>{item.display_name || item.label || displayName(value)}</option>;
                                                })}
                                            </Select>
                                        </div>
                                        {selectedDataset && (
                                            <div className="rounded-lg border border-violet-500/20 bg-slate-950/40 p-3">
                                                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                                                    <div className="min-w-0">
                                                        <div className="flex flex-wrap items-center gap-2">
                                                            <span className="text-sm font-semibold text-slate-200">{selectedDataset.display_name || displayName(form.dataset)}</span>
                                                            <Badge tone={selectedDataset.downloaded ? 'success' : 'neutral'}>
                                                                {isPublicDataset
                                                                    ? 'Managed by AIPerf'
                                                                    : selectedDataset.downloaded ? 'Downloaded' : 'Not downloaded'}
                                                            </Badge>
                                                            {selectedDataset.license && <Badge>{selectedDataset.license}</Badge>}
                                                        </div>
                                                        <p className="mt-1 text-xs text-slate-400">{selectedDataset.description}</p>
                                                        <p className="mt-2 break-all font-mono text-[10px] text-slate-500">
                                                            {form.tracePath || selectedDataset.path}
                                                            {formatBytes(selectedDataset.size_bytes) && ` · ${formatBytes(selectedDataset.size_bytes)}`}
                                                        </p>
                                                    </div>
                                                    {!isPublicDataset && <Button className="w-full sm:w-auto" onClick={downloadDataset} isLoading={downloading} variant={selectedDataset.downloaded ? 'secondary' : 'sky'}>
                                                        <Download className="h-3.5 w-3.5" />
                                                        {selectedDataset.downloaded ? 'Download again' : 'Download dataset'}
                                                    </Button>}
                                                </div>
                                            </div>
                                        )}
                                        {isPublicDataset && (
                                            <div className="rounded-xl border border-violet-500/20 bg-slate-950/60 p-4">
                                                <h3 className="text-sm font-semibold text-slate-200">AIPerf public dataset</h3>
                                                <p className="mt-1 text-[11px] text-slate-500">
                                                    AIPerf downloads and caches this corpus when the task starts. Weka replay supports duration, but not source start/end or scale factor.
                                                </p>
                                                <div className="mt-4 grid gap-4 sm:grid-cols-2">
                                                    <div>
                                                        <Label htmlFor="simulation-public-duration">Duration (seconds)</Label>
                                                        <Input id="simulation-public-duration" type="number" min="1" step="1" value={form.durationSeconds} onChange={(event) => updateDuration(event.target.value)} />
                                                    </div>
                                                    <div>
                                                        <Label htmlFor="simulation-public-scale">Scale factor</Label>
                                                        <Input id="simulation-public-scale" type="number" value="1" disabled />
                                                    </div>
                                                </div>
                                            </div>
                                        )}
                                        {selectedDataset?.downloaded && !isPublicDataset && (
                                            <div className="rounded-xl border border-violet-500/20 bg-slate-950/60 p-4">
                                                <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                                                    <div>
                                                        <h3 className="text-sm font-semibold text-slate-200">Request Timeline</h3>
                                                        <p className="mt-1 text-[11px] text-slate-500">
                                                            {usesDurationRange
                                                                ? 'Drag the end handle below the chart; replay always starts at 0 seconds.'
                                                                : 'Drag either handle below the chart to choose the source replay interval.'}
                                                        </p>
                                                    </div>
                                                    {timeline && (
                                                        <div className="text-right text-[11px] text-slate-400">
                                                            <div><span className="font-semibold text-cyan-300">≈ {formatNumber(selectedTimelineRequests)}</span> selected requests</div>
                                                            <div>{formatNumber(timeline.request_count)} total · {formatBytes(timeline.source_size_bytes)}</div>
                                                        </div>
                                                    )}
                                                </div>
                                                {timelineLoading ? (
                                                    <div className="flex h-64 items-center justify-center gap-2 text-sm text-slate-400">
                                                        <Spinner /> Building request timeline…
                                                    </div>
                                                ) : timelineError ? (
                                                    <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
                                                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {timelineError}
                                                    </div>
                                                ) : timelinePoints.length > 0 ? (
                                                    <>
                                                        <div className="h-72 w-full">
                                                            <ResponsiveContainer width="100%" height="100%">
                                                                <AreaChart data={timelineChartPoints} margin={{ top: 8, right: 12, left: 0, bottom: 4 }}>
                                                                    <defs>
                                                                        <linearGradient id="simulationRequestDensity" x1="0" y1="0" x2="0" y2="1">
                                                                            <stop offset="5%" stopColor="#22d3ee" stopOpacity={0.55} />
                                                                            <stop offset="95%" stopColor="#22d3ee" stopOpacity={0.04} />
                                                                        </linearGradient>
                                                                    </defs>
                                                                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                                                                    <XAxis
                                                                        dataKey="time_seconds"
                                                                        type="number"
                                                                        domain={timelineChartEnd === null
                                                                            ? ['dataMin', 'dataMax']
                                                                            : [0, timelineChartEnd]}
                                                                        tickFormatter={(value) => `${formatNumber(value)}s`}
                                                                        tick={{ fill: '#64748b', fontSize: 10 }}
                                                                        stroke="#334155"
                                                                    />
                                                                    <YAxis
                                                                        allowDecimals={false}
                                                                        width={48}
                                                                        tick={{ fill: '#64748b', fontSize: 10 }}
                                                                        stroke="#334155"
                                                                    />
                                                                    <Tooltip
                                                                        labelFormatter={(value) => `Trace time ${formatNumber(value)}s`}
                                                                        formatter={(value) => [formatNumber(value), 'Requests']}
                                                                        contentStyle={{ background: '#020617', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }}
                                                                    />
                                                                    <Area
                                                                        type="stepAfter"
                                                                        dataKey="request_count"
                                                                        name="Requests"
                                                                        stroke="#22d3ee"
                                                                        fill="url(#simulationRequestDensity)"
                                                                        isAnimationActive={false}
                                                                    />
                                                                    {supportsTraceRange && !usesDurationRange && <Brush
                                                                        dataKey="time_seconds"
                                                                        height={34}
                                                                        stroke="#22d3ee"
                                                                        fill="#0f172a"
                                                                        travellerWidth={10}
                                                                        startIndex={timelineSelection.startIndex}
                                                                        endIndex={timelineSelection.endIndex}
                                                                        onChange={updateTimelineSelection}
                                                                        tickFormatter={(value) => `${formatNumber(value)}s`}
                                                                    />}
                                                                </AreaChart>
                                                            </ResponsiveContainer>
                                                        </div>
                                                        {supportsTraceRange && usesDurationRange && (
                                                            <div className="mt-3 rounded-lg border border-slate-700/70 bg-slate-950/70 px-3 py-3">
                                                                <div className="mb-2 flex items-center justify-between text-[11px] text-slate-400">
                                                                    <span>Fixed start: 0s</span>
                                                                    <span>End: {Math.round(Number(form.traceEndSeconds))}s</span>
                                                                </div>
                                                                <input
                                                                    aria-label="Replay end time"
                                                                    type="range"
                                                                    min="0"
                                                                    max={timelinePoints.length - 1}
                                                                    step="1"
                                                                    value={timelineSelection.endIndex}
                                                                    onChange={(event) => updateTimelineSelection({
                                                                        startIndex: 0,
                                                                        endIndex: Number(event.target.value),
                                                                    })}
                                                                    className="h-2 w-full cursor-pointer accent-cyan-400"
                                                                />
                                                            </div>
                                                        )}
                                                        <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                                                            {supportsTraceRange && <div>
                                                                <div className="text-xs font-medium text-slate-400">Replay start (seconds)</div>
                                                                <div className="mt-2 font-mono text-lg font-semibold text-cyan-300">
                                                                    {Math.round(Number(form.traceStartSeconds))}
                                                                </div>
                                                            </div>}
                                                            {supportsTraceRange && <div>
                                                                <div className="text-xs font-medium text-slate-400">Replay end (seconds)</div>
                                                                <div className="mt-2 font-mono text-lg font-semibold text-cyan-300">
                                                                    {Math.round(Number(form.traceEndSeconds))}
                                                                </div>
                                                            </div>}
                                                            <div>
                                                                <Label htmlFor="simulation-scale">Scale factor</Label>
                                                                <Input
                                                                    id="simulation-scale"
                                                                    type="number"
                                                                    min="0.01"
                                                                    step="0.01"
                                                                    value={form.scaleFactor}
                                                                    disabled={!supportsScaleFactor}
                                                                    onChange={(event) => updateScaleFactor(event.target.value)}
                                                                />
                                                                {!supportsScaleFactor && (
                                                                    <p className="mt-1 text-[11px] text-slate-500">
                                                                        This backend only supports 1x replay for this trace format.
                                                                    </p>
                                                                )}
                                                            </div>
                                                            <div>
                                                                <Label htmlFor="simulation-duration">Duration (seconds)</Label>
                                                                <Input id="simulation-duration" type="number" min="1" step="1" value={form.durationSeconds} onChange={(event) => updateDuration(event.target.value)} />
                                                            </div>
                                                            {form.backend === 'trace-replayer' && (
                                                                <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs leading-relaxed text-amber-100 sm:col-span-2 lg:col-span-4">
                                                                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-300" />
                                                                    <p>
                                                                        Trace-Replayer uses an internal warmup to pre-generate and cache local prompt text for the token lengths in the trace; it does not send warmup requests to the model server. This preparation is included in the duration, so the effective request-issuance window may be shorter and the executed requests can differ from the selected Request Timeline.
                                                                    </p>
                                                                </div>
                                                            )}
                                                            {form.backend === 'aiperf' && supportsTraceRange && (
                                                                <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs leading-relaxed text-amber-100 sm:col-span-2 lg:col-span-4">
                                                                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-300" />
                                                                    <p>
                                                                        AIPerf uses a half-open replay range: start is included, but end is excluded. Requests exactly at the selected end time are not sent, so the executed request count can be lower than the Request Timeline selection.
                                                                    </p>
                                                                </div>
                                                            )}
                                                        </div>
                                                    </>
                                                ) : null}
                                            </div>
                                        )}
                                        <p className="flex items-start gap-2 text-xs leading-relaxed text-violet-200/70">
                                            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                                            Showing datasets compatible with {selectedBackend?.display_name || displayName(form.backend)} and {scenarioDisplayName(form.scenario)}.
                                        </p>
                                    </div>
                                ) : (
                                    <EmptyState
                                        icon={<FileJson className="h-8 w-8" />}
                                        title="No compatible trace datasets"
                                        message="This backend and scenario combination has no registered dataset."
                                    />
                                )}
                            </div>
                        </section>}

                        {wizardStep === 2 && <section aria-labelledby="load-sla-heading">
                            <h2 id="load-sla-heading" className="mb-4 flex items-center gap-2 text-lg font-semibold text-slate-100">
                                <Gauge className="h-5 w-5 text-amber-400" /> Load &amp; SLA Parameters
                            </h2>
                            <div className="grid gap-6 lg:grid-cols-2">
                                <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4">
                                    <h3 className="mb-3 text-sm font-semibold text-amber-300">Replay Parameters</h3>
                                    <div className="grid gap-4 sm:grid-cols-2">
                                        <label className="flex min-h-10 cursor-not-allowed items-center gap-2 text-sm text-slate-300 sm:col-span-2">
                                            <input type="checkbox" checked disabled readOnly className="h-4 w-4 accent-emerald-500" />
                                            Stream responses
                                        </label>
                                    </div>
                                </div>

                                <div className="rounded-lg border border-rose-500/20 bg-rose-500/5 p-4">
                                    <h3 className="mb-3 text-sm font-semibold text-rose-300">SLA Targets</h3>
                                    <div className="grid gap-4 sm:grid-cols-2">
                                        <div>
                                            <Label htmlFor="simulation-ttftSlo">TTFT SLO (milliseconds)</Label>
                                            <Input id="simulation-ttftSlo" type="number" min="0" step="1" value={form.ttftSlo} onChange={(event) => update('ttftSlo', event.target.value)} placeholder="Optional" />
                                        </div>
                                        <div>
                                            <Label htmlFor="simulation-tpotSlo">TPOT SLO (milliseconds)</Label>
                                            <Input id="simulation-tpotSlo" type="number" min="0" step="1" value={form.tpotSlo} onChange={(event) => update('tpotSlo', event.target.value)} placeholder="Optional" />
                                        </div>
                                        <div>
                                            <Label htmlFor="simulation-errorRateSlo">Error Rate SLO (%)</Label>
                                            <Input id="simulation-errorRateSlo" type="number" min="0" max="100" step="0.1" value={form.errorRateSlo} onChange={(event) => update('errorRateSlo', event.target.value)} placeholder="Optional" />
                                        </div>
                                    </div>
                                </div>
                            </div>

                        </section>}

                        {errors.length > 0 && (
                            <div role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-4">
                                <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-rose-200">
                                    <AlertTriangle className="h-4 w-4" /> Check the simulation configuration
                                </div>
                                <ul className="list-disc space-y-1 pl-5 text-xs text-rose-200/80">
                                    {errors.map((error) => <li key={error}>{error}</li>)}
                                </ul>
                            </div>
                        )}
                    </div>
                    </Modal>

                </div>
        </ModulePage>
    );
}
