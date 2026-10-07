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

// -----------------------------------------------------------------------------
// Hand-written half of the Lens MCP tool catalog: tools that are NOT a 1:1
// wrapper over one FastAPI operation (composite/custom logic, or backed by a
// Node/Express route instead of the FastAPI app -- e.g. Deploy PoC,
// remote-deploy, guide planning, candidate search, wait_for_status). Edit
// THIS file directly for anything in `specialTools` below.
//
// This is the actual source of truth for those tools -- there is no FastAPI
// route to regenerate them from. The other half of the catalog
// (`generatedTools`, mechanically derived from the llm_d_bench FastAPI app's
// OpenAPI schema) lives in the fully generated server/mcp/tools.ts, which
// imports shared helpers (PrismTool, toPlainResult, jsonRequest,
// specialTools) from this file. Never hand-edit tools.ts; if a generated
// tool needs to change, edit the route's FastAPI decorator
// (summary/description/operation_id) instead and re-run
// `node scripts/generate-mcp-tools.mjs`.
// -----------------------------------------------------------------------------

import { z } from 'zod';
import { buildQuery, internalRequest } from './internal.ts';

export type ToolRiskTier = 'read' | 'write' | 'approve';

export interface PrismTool {
    name: string;
    description: string;
    riskTier: ToolRiskTier;
    inputShape: z.ZodRawShape;
    // eslint-disable-next-line no-unused-vars -- TypeScript function parameter names.
    handler: (args: Record<string, unknown>) => Promise<unknown>;
}

async function validateDeployment(namespace: string) {
    const result = await internalRequest(`/api/deploy-poc/validate${buildQuery({ namespace })}`);
    return { namespace, ...toPlainResult(result) };
}

export function toPlainResult(result: { ok: boolean; status: number; body: unknown }) {
    if (!result.ok) {
        return { error: `upstream request failed (status ${result.status})`, detail: result.body };
    }
    return result.body as Record<string, unknown>;
}

// Common terminal-state vocabulary used across Lens's own status-check
// tools (deploy jobs, cluster bootstrap, remote deploy, accelerator/cluster
// stack install operations, evaluation runs, model cache sync, simulation
// tasks, ...) -- they all report progress through one of a handful of
// flat, differently-named fields. Used by wait_for_status below.
const TERMINAL_STATUS_KEYWORDS = [
    'ready', 'succeeded', 'success', 'completed', 'complete', 'done', 'finished',
    'failed', 'failure', 'error', 'cancelled', 'canceled', 'timeout', 'timed_out', 'aborted',
];
const STATUS_LIKE_KEYS = ['status', 'phase', 'state', 'ready', 'condition'];

// Best-effort: inspects a small, commonly-used set of status-like top-level
// fields and checks whether the value already looks terminal (ready,
// succeeded, failed, error, ...). Deliberately shallow and conservative: a
// false negative just costs one more (cheap, server-side) poll, but a false
// positive would wrongly end the wait early and hand the model a
// still-in-progress result as if it were final.
function describeTerminalState(result: unknown): string | null {
    if (!result || typeof result !== 'object') return null;
    const record = result as Record<string, unknown>;
    for (const key of STATUS_LIKE_KEYS) {
        const value = record[key];
        if (key === 'ready' && typeof value === 'boolean' && value) return 'ready=true';
        if (typeof value === 'string') {
            const normalized = value.toLowerCase();
            const keyword = TERMINAL_STATUS_KEYWORDS.find((candidate) => normalized.includes(candidate));
            if (keyword) return `${key}=${value}`;
        }
    }
    return null;
}

// Vocabulary for a non-terminal but notable "stuck waiting on external
// resources" state -- most commonly a Kubernetes pod phase of "Pending"
// (see get_deployment_execution_pods) while the scheduler can't yet place
// it, or a deployment run/case still sitting in "queued". Never terminal
// (the operation hasn't failed or succeeded), but also not worth silently
// polling for the full wait_for_status timeout: the caller should be told
// right away so it can surface "resources look insufficient, still queued"
// to the user instead of going quiet for up to 600s.
const PENDING_STATUS_KEYWORDS = ['pending', 'queued', 'unschedulable'];
// Common wrapper keys under which a list of sub-resources (pods, cases,
// items, ...) with their own status-like field may be nested one level
// down from the top-level result. Kept shallow on purpose, same tradeoff
// as describeTerminalState above.
const NESTED_LIST_KEYS = ['pods', 'cases', 'items'];

// Best-effort: looks for a "pending"/"queued"/"unschedulable" status, first
// at the top level (same fields as describeTerminalState), then one level
// down inside common list wrappers (e.g. { pods: [{ phase: "Pending" }] }).
// Only called once describeTerminalState has already ruled out a terminal
// state, so there's no risk of this masking a real success/failure.
export function describePendingState(result: unknown): string | null {
    if (!result || typeof result !== 'object') return null;
    const record = result as Record<string, unknown>;
    const matchKeyword = (value: unknown): string | null => {
        if (typeof value !== 'string') return null;
        const normalized = value.toLowerCase();
        return PENDING_STATUS_KEYWORDS.find((candidate) => normalized.includes(candidate)) ?? null;
    };
    for (const key of STATUS_LIKE_KEYS) {
        const keyword = matchKeyword(record[key]);
        if (keyword) return `${key}=${record[key]}`;
    }
    for (const listKey of NESTED_LIST_KEYS) {
        const list = record[listKey];
        if (!Array.isArray(list)) continue;
        for (const entry of list) {
            if (!entry || typeof entry !== 'object') continue;
            const entryRecord = entry as Record<string, unknown>;
            for (const key of STATUS_LIKE_KEYS) {
                const keyword = matchKeyword(entryRecord[key]);
                if (keyword) return `${listKey}[].${key}=${entryRecord[key]}`;
            }
        }
    }
    return null;
}

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
        setTimeout(resolve, ms);
    });
}

// Generic "poll until terminal, pending, or timeout" loop used by
// wait_for_status. Kept as a standalone, tool-lookup-free function (takes a
// plain `poll` callback instead of a tool name) so it can be unit-tested
// directly with a fake poll function, without needing a real read-tier
// tool's handler or network access.
export async function pollUntilTerminalOrPending(
    poll: () => Promise<unknown>,
    timeoutMs: number,
    intervalMs: number,
): Promise<Record<string, unknown>> {
    const startedAt = Date.now();
    let attempts = 0;
    let lastResult: unknown;
    for (;;) {
        attempts += 1;
        lastResult = await poll();
        const terminal = describeTerminalState(lastResult);
        const elapsedSeconds = Math.round((Date.now() - startedAt) / 1000);
        if (terminal) {
            return { done: true, terminal, attempts, elapsedSeconds, result: lastResult };
        }
        const pending = describePendingState(lastResult);
        if (pending) {
            return {
                done: false,
                pending: true,
                attempts,
                elapsedSeconds,
                result: lastResult,
                message:
                    `Still pending/queued (${pending}) after ${elapsedSeconds}s (${attempts} check(s)). ` +
                    'This usually means the cluster does not yet have free resources to schedule the ' +
                    'workload -- tell the user it looks resource-constrained and queued now instead of ' +
                    'waiting further. Call wait_for_status again with the same arguments only if they ' +
                    'want you to keep monitoring.',
            };
        }
        if (Date.now() - startedAt + intervalMs > timeoutMs) {
            return {
                done: false,
                attempts,
                elapsedSeconds,
                result: lastResult,
                message:
                    `Still not in a terminal state after ${elapsedSeconds}s (${attempts} check(s)). ` +
                    'Call wait_for_status again with the same toolName/toolArguments to keep waiting.',
            };
        }
        await sleep(intervalMs);
    }
}

function buildRepeatedQuery(
    params: Record<string, string | number | undefined | null>,
    repeated: Record<string, string[] | undefined> = {},
): string {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
        if (value === undefined || value === null || value === '') continue;
        search.set(key, String(value));
    }
    for (const [key, values] of Object.entries(repeated)) {
        for (const value of values || []) {
            if (!value) continue;
            search.append(key, value);
        }
    }
    const query = search.toString();
    return query ? `?${query}` : '';
}

export async function jsonRequest(
    path: string,
    method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
    body?: unknown,
    headers?: Record<string, string>,
) {
    return toPlainResult(await internalRequest(path, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }));
}

const jsonRecordSchema = z.record(z.string(), z.unknown());











const candidateSourceSchema = z.object({
    name: z.enum(['aic', 'baseline_search', 'pd_search', 'epd_search', 'tiered_cache_search', 'historical', 'manual']).describe('How the candidate configuration was sourced.'),
    run_id: z.string().optional().describe('Optional originating run id for a historical or search-derived candidate.'),
    result_id: z.string().optional().describe('Optional originating result id for a historical or search-derived candidate.'),
}).passthrough().describe('Provenance descriptor for a candidate configuration.');

const sourceConfigurationSchema = z.object({
    configuration_id: z.string().min(1).describe('Identifier for the source configuration being resolved.'),
    result_id: z.string().optional().describe('Optional originating benchmark result id tied to this configuration.'),
    input_mode: z.enum(['source_result', 'form', 'yaml']).optional().describe('Whether the payload came from an existing result, form input, or raw YAML.'),
    payload: z.union([jsonRecordSchema, z.string()]).describe('Configuration payload to resolve. Use an object for structured form data or a string for YAML text.'),
}).passthrough().describe('One input configuration to resolve into a Lens candidate config.');

const deployableConfigurationSchema = z.object({
    schema_version: z.string().min(1).optional().describe('Optional deployable configuration schema version.'),
    type: z.string().min(1).describe('Deployment configuration type, such as baseline or pd.'),
    format: z.string().min(1).describe('Deployment artifact format, usually helm.'),
    content: jsonRecordSchema.describe('Deployment content object exactly as emitted by Lens Configuration.'),
    provider_ref: z.string().min(1).describe('Deployment provider reference that should consume this configuration.'),
    checksum: z.string().min(1).describe('Checksum Lens uses to identify identical configurations.'),
    provenance: jsonRecordSchema.optional().describe('Optional provenance metadata attached to the configuration.'),
}).passthrough().describe('Immutable deployable configuration accepted by the Deploy module.');

















const remoteTargetSchema = z.object({
    host: z.string().min(1).describe('SSH hostname or IP address of the remote control-plane machine.'),
    port: z.number().int().min(1).max(65535).optional().describe('SSH port. Defaults to 22.'),
    username: z.string().min(1).optional().describe('SSH username Lens should connect as.'),
    authMethod: z.enum(['agent', 'key', 'password']).optional().describe('SSH authentication method Lens should use.'),
    keyPath: z.string().min(1).optional().describe('Server-side private key path when authMethod is key.'),
    hostFingerprint: z.string().min(1).optional().describe('Expected SSH host fingerprint in SHA256:... form.'),
    cards: z.array(z.string().min(1)).optional().describe('Remote accelerator card ids Lens may use for deployment.'),
}).passthrough().describe('Remote SSH target definition for remote deployment tools.');

const remoteCredentialsSchema = z.object({
    password: z.string().optional().describe('SSH password when the target uses password authentication.'),
}).passthrough().describe('Optional runtime credentials for the remote SSH connection.');

const remoteDeployPlanSchema = z.object({
    guide: z.string().min(1).optional().describe('Optional explicit guide name to deploy on the remote cluster.'),
    source: z.string().min(1).optional().describe('Optional candidate source label used to infer a guide when guide is omitted.'),
    candidateId: z.string().optional().describe('Optional source candidate id to preserve in the remote deployment record.'),
    candidateName: z.string().optional().describe('Optional source candidate display name to preserve in the remote deployment record.'),
    name: z.string().min(1).optional().describe('Release postfix or service name for the deployment.'),
    namespace: z.string().min(1).optional().describe('Kubernetes namespace to deploy into on the remote cluster.'),
    model: z.string().min(1).optional().describe('Model name or hf:// reference to deploy.'),
    accelerator: z.string().min(1).optional().describe('Accelerator descriptor. The current backend expects Intel Data Center GPU/XPU.'),
    machineType: z.string().optional().describe('Optional machine type label for UI context.'),
    nodes: z.number().int().min(1).optional().describe('Number of remote nodes. The current backend only supports 1.'),
    replicas: z.number().int().min(1).optional().describe('Decode replica count for aggregate topologies.'),
    prefillTp: z.number().int().min(0).optional().describe('Prefill tensor parallel size for disaggregated topologies.'),
    prefillReplicas: z.number().int().min(0).optional().describe('Prefill replica count for disaggregated topologies.'),
    decodeTp: z.number().int().min(1).optional().describe('Decode tensor parallel size.'),
    decodeReplicas: z.number().int().min(1).optional().describe('Decode replica count.'),
    httpProxy: z.string().optional().describe('Optional HTTP proxy override for the remote deployment command.'),
    httpsProxy: z.string().optional().describe('Optional HTTPS proxy override for the remote deployment command.'),
    noProxy: z.string().optional().describe('Optional NO_PROXY override for the remote deployment command.'),
    deploymentTarget: z.object({
        repository: z.string().min(1).describe('Local path or GitHub repository URL for the llm-d source checkout.'),
        branch: z.string().min(1).optional().describe('Git branch, tag, or ref to check out remotely.'),
        mountPath: z.string().optional().describe('Optional host path to mount as a model cache on the remote host.'),
        mountModelName: z.string().optional().describe('Mounted model directory name to pair with mountPath.'),
    }).passthrough().optional().describe('Repository and cache settings for the remote deployment target.'),
}).passthrough().describe('Remote deployment plan to run over SSH.');

const guidePrepareRequestShape: z.ZodRawShape = {
    guide: z.string().min(1).describe('Guide name to prefetch.'),
    accelerator: z.string().min(1).describe('Accelerator family for the selected guide.'),
    modelServer: z.string().min(1).describe('Model server for the selected guide.'),
    guideVariant: z.string().optional().describe('Optional guide variant name to prefetch.'),
};

const guidePlanRequestShape: z.ZodRawShape = {
    guide: z.string().min(1).describe('Guide name to render and plan.'),
    accelerator: z.string().min(1).describe('Accelerator family for the selected guide.'),
    modelServer: z.string().min(1).describe('Model server for the selected guide.'),
    model: z.string().min(1).describe('Model name or repo id the guide should serve.'),
    clusterSessionId: z.string().min(36).max(36).optional().describe('Optional active cluster session id to validate against.'),
    guideVariant: z.string().optional().describe('Optional guide variant to select within the catalog.'),
    environment: z.object({
        mode: z.enum(['current', 'remote']).optional().describe('Whether to inspect the current machine or a remote target.'),
        kubernetesMode: z.enum(['auto', 'required', 'disabled']).optional().describe('How strongly the planner should require a reachable Kubernetes cluster.'),
        target: remoteTargetSchema.optional().describe('Remote target to inspect when environment.mode is remote.'),
        credentials: remoteCredentialsSchema.optional().describe('Optional credentials to use when environment.mode is remote.'),
    }).passthrough().optional().describe('Execution environment Lens should inspect while planning.'),
    source: z.object({
        mode: z.enum(['official', 'local', 'remote']).optional().describe('Where the guide manifests should be loaded from.'),
        ref: z.string().optional().describe('Optional local or remote source ref.'),
        repository: z.string().optional().describe('Optional local path or repository URL for a custom guide source.'),
        url: z.string().url().optional().describe('Optional remote manifest URL when using a remote guide source.'),
    }).passthrough().optional().describe('Optional custom guide source instead of the official catalog.'),
    customPatches: z.array(jsonRecordSchema).optional().describe('Optional custom patch objects to apply to the rendered manifests before validation.'),
};

const candidateSupportShape: z.ZodRawShape = {
    workload: z.object({
        model: z.string().min(1).describe('Model name to check.'),
        isl: z.number().int().min(1).optional().describe('Mean input sequence length in tokens.'),
        osl: z.number().int().min(1).optional().describe('Mean output sequence length in tokens.'),
        ttftMs: z.number().positive().optional().describe('Optional TTFT target in milliseconds.'),
        tpotMs: z.number().positive().optional().describe('Optional TPOT target in milliseconds.'),
    }).passthrough().describe('Workload description used to check AIConfigurator support.'),
    searchConfig: z.object({
        totalGpus: z.number().int().min(1).max(64).optional().describe('Total accelerator count to search against.'),
        aicSystemName: z.string().min(1).optional().describe('AIConfigurator system name, such as b60.'),
        aicBackendName: z.string().min(1).optional().describe('AIConfigurator backend name, such as vllm.'),
        aicDatabaseMode: z.string().min(1).optional().describe('AIConfigurator database mode, such as SILICON.'),
    }).passthrough().describe('Hardware and search settings for the AIConfigurator support check.'),
};

const candidateSearchShape: z.ZodRawShape = {
    workload: candidateSupportShape.workload,
    searchConfig: z.object({
        totalGpus: z.number().int().min(1).max(64).optional().describe('Total accelerator count to search against.'),
        maxCandidatesPerMode: z.number().int().min(1).max(50).optional().describe('Maximum number of candidates to keep per source or topology mode.'),
        aicSystemName: z.string().min(1).optional().describe('AIConfigurator system name, such as b60.'),
        aicBackendName: z.string().min(1).optional().describe('AIConfigurator backend name, such as vllm.'),
        aicDatabaseMode: z.string().min(1).optional().describe('AIConfigurator database mode, such as SILICON.'),
    }).passthrough().describe('Hardware and search settings for candidate generation.'),
    sourceIds: z.array(z.enum(['aic', 'manual', 'baseline', 'pd', 'epd', 'tiered-cache', 'precise-prefix-cache'])).min(1).describe('Candidate source ids Lens should include in the search.'),
    manualConfig: z.object({
        inputMode: z.enum(['form', 'yaml']).optional().describe('Whether the manual candidate comes from structured form fields or an AIConfigurator YAML file.'),
        yamlText: z.string().optional().describe('AIConfigurator experiment YAML text when inputMode is yaml.'),
        name: z.string().optional().describe('Optional display name for a manual estimate.'),
        servingMode: z.enum(['agg', 'disagg']).optional().describe('Manual serving mode for AIConfigurator estimate requests.'),
        tp: z.number().int().min(1).optional().describe('Aggregate tensor parallel size.'),
        pp: z.number().int().min(1).optional().describe('Aggregate pipeline parallel size.'),
        replicas: z.number().int().min(1).optional().describe('Aggregate replica count.'),
        batchSize: z.number().int().min(1).optional().describe('Aggregate batch size.'),
        prefillTp: z.number().int().min(1).optional().describe('Prefill tensor parallel size for disaggregated estimates.'),
        prefillReplicas: z.number().int().min(1).optional().describe('Prefill replica count for disaggregated estimates.'),
        prefillBatchSize: z.number().int().min(1).optional().describe('Prefill batch size for disaggregated estimates.'),
        decodeTp: z.number().int().min(1).optional().describe('Decode tensor parallel size for disaggregated estimates.'),
        decodeReplicas: z.number().int().min(1).optional().describe('Decode replica count for disaggregated estimates.'),
        decodeBatchSize: z.number().int().min(1).optional().describe('Decode batch size for disaggregated estimates.'),
    }).passthrough().optional().describe('Optional manual candidate or YAML experiment input.'),
};

const estimateCapacityShape: z.ZodRawShape = {
    model: z.string().min(1).describe('HuggingFace model ID or local model cache path (e.g. meta-llama/Llama-3-8B).'),
    gpu_memory_gib: z.number().positive().describe('Per-device GPU VRAM in GiB (e.g. 80 for H100/A100, 32 for B60).'),
    tensor_parallel_size: z.number().int().min(1).optional().describe('Tensor parallelism degree (default 1).'),
    max_model_len: z.number().int().min(256).optional().describe('Maximum sequence context length in tokens (default 4096).'),
    gpu_memory_utilization: z.number().positive().max(1).optional().describe('GPU memory utilization factor between 0.0 and 1.0 (default 0.9).'),
    pipeline_parallel_size: z.number().int().min(1).optional().describe('Pipeline parallelism degree (default 1).'),
    replicas: z.number().int().min(1).optional().describe('Number of model replicas (default 1).'),
    model_weight_gib: z.number().positive().optional().describe('Optional known model weight in GiB if not inferable from config.'),
};

// Hand-written tools: each is either backed by a Node/Express route (not the
// llm_d_bench FastAPI app, so it has no OpenAPI operation to generate from) or
// wraps custom composite logic (e.g. wait_for_status polls another tool, others
// reshape/aggregate a response) that a generic REST-wrapper generator cannot
// produce. Maintained by hand; NOT touched by scripts/generate-mcp-tools.mjs.
export const specialTools: PrismTool[] = [
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'list_deploy_poc_configs',
        description: 'List the built-in Lens Deploy PoC configuration presets. Use this to discover the supported local smoke or known-good deploy modes before starting a PoC deployment.',
        riskTier: 'read',
        inputShape: {},
        handler: async () => toPlainResult(await internalRequest('/api/deploy-poc/config')),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'start_deploy_poc',
        description: 'Start a local Lens Deploy PoC deployment. Use config=smoke-pause for a tiny smoke deployment or the known-good preset for the legacy llm-d guide path.',
        riskTier: 'approve',
        inputShape: {
            config: z.string().min(1).optional().describe('PoC configuration id to start. Omit to use the known-good preset.'),
            namespace: z.string().min(1).max(63).optional().describe('Kubernetes namespace to deploy into. Defaults to the preset\'s namespace.'),
            clusterSessionId: z.string().min(36).max(36).optional().describe('Optional active Lens cluster session id whose kubeconfig should be used.'),
        },
        handler: async (args) => jsonRequest('/api/deploy-poc/start', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'teardown_deploy_poc',
        description: 'Tear down a local Lens Deploy PoC deployment and optionally delete its namespace. Use this for cleanup of legacy PoC runs.',
        riskTier: 'approve',
        inputShape: {
            config: z.string().min(1).optional().describe('PoC configuration id that was originally started. Omit to use the known-good preset.'),
            namespace: z.string().min(1).max(63).optional().describe('Kubernetes namespace to delete or destroy.'),
            clusterSessionId: z.string().min(36).max(36).optional().describe('Optional active Lens cluster session id whose kubeconfig should be used.'),
        },
        handler: async (args) => jsonRequest('/api/deploy-poc/teardown', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'resolve_configurations',
        description: 'Resolve one or more candidate configurations into Lens candidate config objects and validations. This uses the local Lens proxy route that the frontend calls.',
        riskTier: 'write',
        inputShape: {
            candidate_source: candidateSourceSchema.describe('Provenance descriptor for the candidate configurations being resolved.'),
            configurations: z.array(sourceConfigurationSchema).min(1).describe('Source configuration payloads to resolve.'),
            target: z.object({
                cluster_id: z.string().min(1).optional().describe('Optional target cluster id used during resolution.'),
            }).passthrough().optional().describe('Optional target cluster context for configuration resolution.'),
        },
        handler: async (args) => jsonRequest('/api/configurations/resolve', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'render_configuration',
        description: 'Render a resolved candidate configuration into a deployable Lens manifest. Use this after resolve_configurations or guide planning when you want an immutable deployment artifact.',
        riskTier: 'write',
        inputShape: {
            candidate_config: jsonRecordSchema.describe('Resolved candidate configuration object to render.'),
            render: z.object({
                format: z.literal('manifest').optional().describe('Render output format. Lens currently expects manifest.'),
                guide_ref: z.string().min(1).describe('Guide identifier used to produce the rendered deployment.'),
                template_ref: z.string().min(1).describe('Template or provider reference used during rendering.'),
                guide_source: z.object({
                    repository: z.string().min(1).describe('Source repository for the guide.'),
                    requestedRef: z.string().min(1).describe('Requested Git ref for the guide source.'),
                    commit: z.string().length(40).describe('Resolved guide source commit SHA.'),
                    guide: z.string().min(1).describe('Guide name used for rendering.'),
                    accelerator: z.string().min(1).describe('Accelerator family for the guide.'),
                    modelServer: z.string().min(1).describe('Model server selected for the guide.'),
                    variant: z.string().min(1).describe('Guide variant name.'),
                    files: z.array(z.string().min(1)).min(1).describe('Manifest file paths included in the rendered guide source.'),
                }).passthrough().describe('Guide source metadata tied to the rendered artifact.'),
                rendered_manifest: z.string().min(1).describe('Rendered Kubernetes manifest YAML.'),
                cluster_ref: z.object({
                    id: z.string().min(1).describe('Target cluster id.'),
                    name: z.string().min(1).describe('Target cluster display name.'),
                    session_id: z.string().min(36).max(36).describe('Target cluster session id.'),
                    connection: z.literal('managed-kubeconfig').optional().describe('Cluster connection type.'),
                }).passthrough().describe('Resolved cluster reference for the deployment target.'),
                deployment: z.object({
                    readinessDeployments: z.array(z.string().min(1)).min(1).describe('Deployment names Lens should watch for readiness.'),
                    endpoint: z.object({
                        protocol: z.enum(['http', 'https']).describe('Protocol used by the exposed deployment endpoint.'),
                        serviceName: z.string().min(1).describe('Kubernetes Service name that exposes the deployment.'),
                        port: z.number().int().min(1).max(65535).describe('Service port for the deployment endpoint.'),
                    }).describe('Endpoint contract for the deployment.'),
                }).passthrough().describe('Deployment readiness and endpoint contract.'),
                modelSecret: z.object({
                    mode: z.enum(['none', 'host', 'existing-secret']).optional().describe('How runtime Hugging Face credentials should be sourced.'),
                    sourceNamespace: z.string().min(1).optional().describe('Source namespace when mode is existing-secret.'),
                    sourceName: z.string().min(1).optional().describe('Source Secret name when mode is existing-secret.'),
                }).passthrough().optional().describe('Model secret binding to attach during rendering.'),
                deploymentName: z.string().min(1).max(120).optional().describe('Optional deployment display name for the final artifact.'),
            }).passthrough().describe('Render contract describing source guide, cluster, and endpoint expectations.'),
        },
        handler: async (args) => jsonRequest('/api/configurations/render', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'save_configuration',
        description: 'Publish a deployable configuration as a Lens configuration artifact. Use this when you want a stable artifact id that other deployment or evaluation flows can reference.',
        riskTier: 'write',
        inputShape: {
            deployable_configuration: deployableConfigurationSchema.describe('Deployable configuration to save as a published artifact.'),
            file: z.object({
                name: z.string().min(1).optional().describe('Optional output filename to use for the saved configuration artifact.'),
            }).passthrough().optional().describe('Optional saved-file settings.'),
        },
        handler: async (args) => jsonRequest('/api/configurations/save', 'POST', args),
    },
    // hand-written: composite/custom logic (complex:buildRepeatedQuery)
    {
        name: 'get_storage_volume_resource_status',
        description: 'Get Kubernetes-side resource status for one or more storage volumes. Use this to inspect PVC, PV, or hostPath backing health for registered volumes.',
        riskTier: 'read',
        inputShape: {
            volumeIds: z.array(z.string().min(1)).min(1).describe('One or more storage volume ids to inspect.'),
        },
        handler: async ({ volumeIds }) =>
            toPlainResult(
                await internalRequest(
                    `/api/v1/storage/volume-resource-status${buildRepeatedQuery({}, { volume_id: (volumeIds as string[]) || [] })}`,
                ),
            ),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'get_remote_deploy_defaults',
        description: 'Get the server-side default repository, branch, proxy, and model-mount settings Lens will use for remote SSH deployments.',
        riskTier: 'read',
        inputShape: {},
        handler: async () => toPlainResult(await internalRequest('/api/remote-deploy/defaults')),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'get_remote_deploy_fingerprint',
        description: 'Fetch the SSH host fingerprint for a remote deployment target without authenticating. Use this before test or start calls so the fingerprint can be pinned explicitly.',
        riskTier: 'read',
        inputShape: {
            target: z.object({
                host: z.string().min(1).describe('SSH hostname or IP address to probe.'),
                port: z.number().int().min(1).max(65535).optional().describe('SSH port to probe. Defaults to 22.'),
                username: z.string().min(1).optional().describe('Optional probe username. Defaults to probe on the backend.'),
            }).passthrough().describe('Remote SSH target to fingerprint.'),
        },
        handler: async (args) => jsonRequest('/api/remote-deploy/fingerprint', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'test_remote_deploy_target',
        description: 'Test SSH connectivity and basic remote prerequisites for a remote deployment target. Use this before attempting a real remote deployment.',
        riskTier: 'write',
        inputShape: {
            target: remoteTargetSchema.describe('Remote SSH target to test.'),
            credentials: remoteCredentialsSchema.optional().describe('Optional runtime credentials for the remote target.'),
        },
        handler: async (args) => jsonRequest('/api/remote-deploy/test', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'start_remote_deploy',
        description: 'Start a remote SSH-driven deployment using the remote deploy backend. Use this when Lens should run llm-d-bench on a remote control-plane machine instead of the local cluster session.',
        riskTier: 'approve',
        inputShape: {
            target: remoteTargetSchema.describe('Remote SSH target to deploy onto.'),
            credentials: remoteCredentialsSchema.optional().describe('Optional runtime credentials for the remote target.'),
            plan: remoteDeployPlanSchema.describe('Remote deployment plan describing model, topology, repository, and namespace choices.'),
        },
        handler: async (args) => jsonRequest('/api/remote-deploy/start', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'get_remote_deploy_status',
        description: 'Get the live pod and endpoint status for a remote SSH deployment namespace.',
        riskTier: 'read',
        inputShape: {
            target: remoteTargetSchema.describe('Remote SSH target whose cluster status should be queried.'),
            credentials: remoteCredentialsSchema.optional().describe('Optional runtime credentials for the remote target.'),
            namespace: z.string().min(1).max(63).describe('Remote Kubernetes namespace to inspect.'),
        },
        handler: async (args) => jsonRequest('/api/remote-deploy/status', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'get_remote_deploy_logs',
        description: 'Get logs from a remote SSH deployment namespace, optionally scoped to one pod.',
        riskTier: 'read',
        inputShape: {
            target: remoteTargetSchema.describe('Remote SSH target whose deployment logs should be queried.'),
            credentials: remoteCredentialsSchema.optional().describe('Optional runtime credentials for the remote target.'),
            namespace: z.string().min(1).max(63).describe('Remote Kubernetes namespace to inspect.'),
            pod: z.string().min(1).optional().describe('Optional pod name to restrict logs to one pod.'),
            tail: z.number().int().min(1).max(1000).optional().describe('Maximum number of log lines to return.'),
        },
        handler: async (args) => jsonRequest('/api/remote-deploy/logs', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'teardown_remote_deploy',
        description: 'Tear down a remote SSH deployment namespace and its llm-d resources.',
        riskTier: 'approve',
        inputShape: {
            target: remoteTargetSchema.describe('Remote SSH target whose deployment should be torn down.'),
            credentials: remoteCredentialsSchema.optional().describe('Optional runtime credentials for the remote target.'),
            namespace: z.string().min(1).max(63).describe('Remote Kubernetes namespace to destroy.'),
        },
        handler: async (args) => jsonRequest('/api/remote-deploy/teardown', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'prepare_guide_plan',
        description: 'Prefetch and validate the manifest set for a selected guide, accelerator, model server, and optional variant. Use this as a lightweight readiness check before full planning.',
        riskTier: 'write',
        inputShape: guidePrepareRequestShape,
        handler: async (args) => jsonRequest('/api/guide-planning/prepare', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'plan_guide_deployment',
        description: 'Render, adapt, and validate a guide-backed deployment plan for a specific model and environment. Use this to generate deployment-ready manifests plus planning metadata.',
        riskTier: 'write',
        inputShape: guidePlanRequestShape,
        handler: async (args) => jsonRequest('/api/guide-planning/plan', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'check_candidate_support',
        description: 'Check whether AIConfigurator supports a workload on the requested hardware profile. This calls Lens\'s local candidate-support wrapper rather than the raw backend endpoint.',
        riskTier: 'write',
        inputShape: candidateSupportShape,
        handler: async (args) => jsonRequest('/api/candidate-support', 'POST', args),
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'search_candidates',
        description: 'Generate candidate serving configurations from one or more search sources, including AIC predictions, manual estimates, and local grid heuristics. This calls Lens\'s local candidate-search wrapper.',
        riskTier: 'write',
        inputShape: candidateSearchShape,
        handler: async (args) => jsonRequest('/api/candidate-search', 'POST', args),
    },
    // capacity estimation engine: pure in-tree mathematical planning and validation
    {
        name: 'estimate_vllm_capacity',
        description:
            'Estimate vLLM GPU memory breakdown (weights, activation, CUDA graph, KV cache), ' +
            'verify tensor parallel compatibility, and calculate maximum concurrent requests ' +
            'for a model on target accelerator hardware.',
        riskTier: 'read',
        inputShape: estimateCapacityShape,
        handler: async (args) => jsonRequest('/api/v1/capacity/estimate', 'POST', args),
    },
    {
        name: 'search_aic_with_relaxation',
        description: 'Search AIC at original latency targets, then relax to 1.25x, 1.5x, 1.75x and 2x (at most five attempts) until a complete anchor using exactly the selected GPU capacity is found. Returns attempts and the closest anchor; estimates are not SLO proof.',
        riskTier: 'read',
        inputShape: {
            clusterId: z.string().min(1),
            workload: candidateSupportShape.workload,
            searchConfig: candidateSearchShape.searchConfig,
        },
        handler: async (args) => {
            const response = await internalRequest(`/api/cluster/overview${buildQuery({clusterId: args.clusterId as string})}`);
            if (!response.ok) throw new Error(`Cluster overview failed (status ${response.status})`);
            const overview = toPlainResult(response);
            const nested = overview.kubernetes as Record<string, unknown> | undefined;
            const hardware = (overview.hardware ?? nested?.hardware) as Record<string, unknown> | undefined;
            const available = Number(hardware?.availableGpuCount);
            const total = Number(hardware?.gpuCount);
            const requested = (args.searchConfig ?? {}) as Record<string, unknown>;
            const mode = String(requested.capacityMode ?? 'free');
            const budget = mode === 'all' ? total : mode === 'custom' ? Number(requested.customGpuCount) : available;
            if (!Number.isInteger(budget) || budget < 1 || budget > 64 || budget > total) {
                throw new Error('Cluster overview has no valid AIC search GPU budget');
            }
            return jsonRequest('/api/candidate-search/relaxed', 'POST', {
                workload: args.workload,
                searchConfig: {...requested, totalGpus: budget},
                sourceIds: ['aic'],
            });
        },
    },
    // hand-written: composite/custom logic (complex:const)
    {
        name: 'get_deploy_status',
        description:
            'Get the live job + Kubernetes status of a Lens Deploy PoC deployment by namespace ' +
            '(merges the local deploy job phase with real pod status from the OptimalBench service).',
        riskTier: 'read',
        inputShape: {
            namespace: z.string().min(1).max(63).optional().describe('Kubernetes namespace of the deployment. Defaults to the known-good PoC namespace.'),
        },
        handler: async ({ namespace }) => {
            const result = await internalRequest(`/api/deploy-poc/status${buildQuery({ namespace: namespace as string | undefined })}`);
            return toPlainResult(result);
        },
    },
    // hand-written: composite/custom logic (0-calls)
    {
        name: 'get_deploy_validation',
        description:
            'Check whether a Lens Deploy PoC deployment is fully ready (all pods Ready, gateway endpoint present) ' +
            'and return its OpenAI-compatible gateway endpoint if so.',
        riskTier: 'read',
        inputShape: {
            namespace: z.string().min(1).max(63).optional().describe('Kubernetes namespace of the deployment. Defaults to the known-good PoC namespace.'),
        },
        handler: async ({ namespace }) => validateDeployment((namespace as string | undefined) ?? ''),
    },
    // hand-written: composite/custom logic (complex:if ()
    {
        name: 'list_ready_deployments',
        description:
            'List real Lens deployments (created via Model Market / Deploy) that are Ready and expose an ' +
            'OpenAI-compatible gateway endpoint. Optionally scope to one cluster. Use this to discover which ' +
            'Deployment can be selected as the model service for a Playground conversation.',
        riskTier: 'read',
        inputShape: {
            clusterId: z.string().min(1).max(64).optional().describe('Restrict results to deployments on this cluster id (as returned by /api/cluster/clusters).'),
        },
        handler: async ({ clusterId }) => {
            const result = await internalRequest(
                `/api/v1/deployments/executions${buildQuery({ cluster_id: clusterId as string | undefined, status: 'ready' })}`,
            );
            const body = toPlainResult(result) as { items?: Record<string, unknown>[]; error?: string };
            if (body.error) return { deployments: [], error: body.error };
            const deployments = (body.items || []).map((item) => ({
                id: String(item.execution_id ?? ''),
                source: 'deployment',
                namespace: (item.namespace as string | null) ?? null,
                clusterId: (item.cluster_id as string | null) ?? null,
                model: (item.model as string | null) ?? null,
                ready: item.status === 'ready',
                gatewayEndpoint: (item.endpoint as string | null) ?? (item.forwarded_endpoint as string | null) ?? null,
            }));
            return { deployments };
        },
    },
    // hand-written: backed by a Node/Express route, not FastAPI
    {
        name: 'get_guide_catalog',
        description: 'List the deployable guide catalog (provider / accelerator / model server / variant combinations Lens knows how to deploy). ' +
            'Requires a cluster id (or an active cluster session id) whose llm-d Software Versions have already been downloaded -- ' +
            'the catalog is built from that cluster\'s downloaded repo, it is not global.',
        riskTier: 'read',
        inputShape: {
            clusterId: z.string().min(1).max(64).optional().describe('Cluster id (as returned by /api/cluster/clusters) whose downloaded Software Versions define the catalog.'),
            clusterSessionId: z.string().min(36).max(36).optional().describe('Optional active Lens cluster session id to use instead of clusterId.'),
        },
        handler: async ({ clusterId, clusterSessionId }) => toPlainResult(
            await internalRequest(`/api/guide-planning/catalog${buildQuery({ clusterId: clusterId as string | undefined, clusterSessionId: clusterSessionId as string | undefined })}`),
        ),
    },
    // hand-written: composite/custom logic (0-calls)
    {
        name: 'wait_for_status',
        description:
            'Repeatedly poll another READ-tier status-check tool (e.g. get_cluster_bootstrap_status, ' +
            'get_remote_deploy_status, get_deployment_execution, get_accelerator_monitoring_operation, ' +
            'get_cluster_stack_operation, get_deploy_status, get_deploy_validation, get_model_cache_entry, ' +
            'get_evaluate_run, get_simulation_task, get_evaluation_workflow) SERVER-SIDE until it reaches a ' +
            'terminal state (ready/succeeded/failed/error/...) or a timeout elapses, instead of you calling ' +
            'that tool yourself over and over across many separate turns. Always prefer this over manually ' +
            're-invoking a status tool in a loop while waiting for a deploy, install, bootstrap, evaluation, ' +
            'or similar long-running operation to finish. If it times out before reaching a terminal state, ' +
            'call wait_for_status again with the same arguments to keep waiting from where it left off. ' +
            'If the polled resource looks "pending"/"queued"/"unschedulable" (e.g. a Kubernetes pod phase of ' +
            '"Pending"), this tool returns immediately (pending: true) instead of consuming the whole ' +
            'timeout -- this usually means the cluster does not yet have free resources to schedule the ' +
            'workload, so tell the user it looks resource-constrained and the request is queued right away ' +
            'rather than waiting for it to finish; only call wait_for_status again if they want you to keep ' +
            'monitoring.',
        riskTier: 'read',
        inputShape: {
            toolName: z.string().min(1).describe('Name of an existing READ-tier Lens tool that reports a status (e.g. "get_cluster_bootstrap_status").'),
            toolArguments: jsonRecordSchema.optional().describe('Arguments to call that tool with, same shape as calling it directly. Omit if it takes none.'),
            timeoutSeconds: z.number().min(1).max(600).optional().describe('How long to keep polling before giving up and returning the latest status. Default 60, max 600.'),
            intervalSeconds: z.number().min(1).max(30).optional().describe('Seconds to wait between polls. Default 5, max 30.'),
        },
        handler: async ({ toolName, toolArguments, timeoutSeconds, intervalSeconds }) => {
            const targetName = String(toolName ?? '');
            const target = findTool(targetName);
            if (!target) {
                return { error: `unknown tool "${targetName}"` };
            }
            if (target.riskTier !== 'read') {
                // Never let this become a backdoor for repeatedly triggering
                // a write/approve-tier action outside the normal
                // confirm/reject flow -- polling is for status checks only.
                return { error: `"${targetName}" is a ${target.riskTier}-tier tool; wait_for_status can only poll read-tier tools` };
            }
            const timeoutMs = Math.min(Math.max(Number(timeoutSeconds) || 60, 1), 600) * 1000;
            const intervalMs = Math.min(Math.max(Number(intervalSeconds) || 5, 1), 30) * 1000;
            const args = (toolArguments as Record<string, unknown> | undefined) ?? {};
            return pollUntilTerminalOrPending(() => target.handler(args), timeoutMs, intervalMs);
        },
    },
];

// The full tool catalog, both halves: this file's hand-written specialTools
// plus tools.ts's generated `generatedTools`. Starts out holding only
// specialTools; tools.ts pushes its generated half into this SAME array once
// it finishes loading (see the bottom of that file), then re-exports it.
// Declared here -- not in tools.ts -- so hand-written tools above (e.g.
// wait_for_status, which must look up ANY tool by name, generated or
// hand-written) can call findTool without a circular import between the two
// files. Safe because handlers only ever call findTool() at request time,
// long after both modules have finished loading.
export const prismTools: PrismTool[] = [...specialTools];

export function findTool(name: string): PrismTool | undefined {
    return prismTools.find((tool) => tool.name === name);
}
