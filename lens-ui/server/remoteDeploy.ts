// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import express from 'express';
import { Client, type ConnectConfig } from 'ssh2';
import { quote as quoteShellArguments } from 'shell-quote';

export const remoteDeployRouter = express.Router();

const DEFAULT_DEPLOY_BIN = process.env.PRISM_REMOTE_DEPLOY_BIN || 'llm-d-bench';
const REMOTE_DEPLOY_ENABLED = process.env.PRISM_REMOTE_DEPLOY_ENABLED === 'true'
    || process.env.NODE_ENV !== 'production';
const REMOTE_HOST_ALLOWLIST = (process.env.PRISM_REMOTE_HOST_ALLOWLIST || '').split(',').map((host) => host.trim()).filter(Boolean);
const SSH_KEY_DIR = path.resolve(process.env.PRISM_SSH_KEY_DIR || path.join(process.env.HOME || '', '.ssh'));

const HOST_RE = /^(?=.{1,253}$)(?:\[[0-9a-fA-F:]+\]|[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?)$/;
const USER_RE = /^[a-z_][a-z0-9_-]{0,31}$/i;
const NAMESPACE_RE = /^(?=.{1,63}$)[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
const CARD_RE = /^[0-9]{1,3}$/;
const POD_RE = /^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$/;
const BRANCH_RE = /^[A-Za-z0-9._/-]{1,128}$/;
const TERMINAL_REASONS = new Set(['CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError', 'RunContainerError']);

const remoteTargetDefaults = () => ({
    repository: process.env.LLM_D_ROOT || process.env.LLM_D_REPOSITORY || '',
    branch: process.env.LLM_D_BRANCH || '',
    httpProxy: process.env.HTTP_PROXY || process.env.LLM_D_HTTP_PROXY || '',
    httpsProxy: process.env.HTTPS_PROXY || process.env.LLM_D_HTTPS_PROXY || '',
    noProxy: process.env.NO_PROXY || process.env.LLM_D_NO_PROXY || '',
    mountPath: process.env.LLM_D_MOUNT_PATH || '',
    mountModelName: process.env.LLM_D_MOUNT_MODEL_NAME || '',
});

type RemoteTarget = {
    host: string;
    port: number;
    username: string;
    authMethod: 'agent' | 'key' | 'password';
    keyPath?: string;
    hostFingerprint: string;
    cards: string[];
};

type RemoteCredentials = { password?: string };

type ExecResult = { stdout: string; stderr: string; code: number };

function assertEnabled() {
    if (!REMOTE_DEPLOY_ENABLED) {
        throw Object.assign(new Error('Remote deployment is disabled on this server'), { status: 503 });
    }
}

function parseTarget(body: unknown, requireFingerprint = true, requireCards = true): RemoteTarget {
    const raw = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
    const host = String(raw.host || '').trim();
    const port = Number(raw.port || 22);
    const username = String(raw.username || '').trim();
    const authMethod = String(raw.authMethod || 'agent') as RemoteTarget['authMethod'];
    const keyPath = String(raw.keyPath || '').trim() || undefined;
    const hostFingerprint = String(raw.hostFingerprint || '').trim();
    const cards = Array.isArray(raw.cards) ? raw.cards.map(String) : [];

    if (!HOST_RE.test(host)) throw Object.assign(new Error('Invalid SSH host'), { status: 400 });
    if (process.env.NODE_ENV === 'production' && !REMOTE_HOST_ALLOWLIST.includes(host)) {
        throw Object.assign(new Error('SSH host is not in PRISM_REMOTE_HOST_ALLOWLIST'), { status: 403 });
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw Object.assign(new Error('Invalid SSH port'), { status: 400 });
    if (!USER_RE.test(username)) throw Object.assign(new Error('Invalid SSH username'), { status: 400 });
    if (!['agent', 'key', 'password'].includes(authMethod)) throw Object.assign(new Error('Invalid authentication method'), { status: 400 });
    if (authMethod === 'key' && !keyPath) throw Object.assign(new Error('A server-side private key path is required'), { status: 400 });
    if (requireFingerprint && !/^SHA256:[A-Za-z0-9+/]{43}=?$/.test(hostFingerprint)) {
        throw Object.assign(new Error('A valid SSH host fingerprint is required'), { status: 400 });
    }
    if (requireCards && (!cards.length || cards.some((card) => !CARD_RE.test(card)))) {
        throw Object.assign(new Error('Select at least one valid accelerator card'), { status: 400 });
    }
    return { host, port, username, authMethod, keyPath, hostFingerprint, cards: [...new Set(cards)] };
}

function fingerprint(key: Buffer): string {
    return `SHA256:${crypto.createHash('sha256').update(key).digest('base64').replace(/=+$/, '')}`;
}

function connectConfig(target: RemoteTarget, credentials: RemoteCredentials): ConnectConfig {
    const config: ConnectConfig = {
        host: target.host,
        port: target.port,
        username: target.username,
        readyTimeout: 15_000,
        keepaliveInterval: 10_000,
        hostHash: 'sha256',
        hostVerifier: (hashedKey) => `SHA256:${hashedKey.replace(/=+$/, '')}` === target.hostFingerprint.replace(/=+$/, ''),
    };
    if (target.authMethod === 'password') {
        if (!credentials.password) throw Object.assign(new Error('Password is required'), { status: 400 });
        config.password = credentials.password;
    } else if (target.authMethod === 'key') {
        const expanded = target.keyPath!.replace(/^~(?=\/)/, process.env.HOME || '');
        const resolved = path.resolve(expanded);
        const relative = path.relative(SSH_KEY_DIR, resolved);
        if (relative.startsWith('..') || path.isAbsolute(relative)) {
            throw Object.assign(new Error(`Private key must be inside ${SSH_KEY_DIR}`), { status: 400 });
        }
        config.privateKey = fs.readFileSync(resolved);
    } else {
        if (!process.env.SSH_AUTH_SOCK) throw Object.assign(new Error('SSH_AUTH_SOCK is not available to the Lens server'), { status: 400 });
        config.agent = process.env.SSH_AUTH_SOCK;
    }
    return config;
}

// eslint-disable-next-line no-unused-vars
function withConnection<T>(config: ConnectConfig, operation: (client: Client) => Promise<T>): Promise<T> {
    return new Promise((resolve, reject) => {
        const client = new Client();
        let settled = false;
        const finish = (fn: typeof resolve | typeof reject, value: T | unknown) => {
            if (settled) return;
            settled = true;
            client.end();
            fn(value as T);
        };
        client.once('ready', () => operation(client).then((value) => finish(resolve, value), (error) => finish(reject, error)));
        client.once('error', (error) => finish(reject, error));
        client.connect(config);
    });
}

function execRemote(client: Client, command: string): Promise<ExecResult> {
    return new Promise((resolve, reject) => {
        client.exec(command, (error, stream) => {
            if (error) return reject(error);
            let stdout = '';
            let stderr = '';
            const append = (current: string, chunk: Buffer) => `${current}${chunk.toString()}`.slice(-32_000);
            stream.on('data', (chunk: Buffer) => { stdout = append(stdout, chunk); });
            stream.stderr.on('data', (chunk: Buffer) => { stderr = append(stderr, chunk); });
            stream.once('close', (code: number | null) => resolve({ stdout, stderr, code: code ?? -1 }));
            stream.once('error', reject);
        });
    });
}

export async function executeRemoteInspection(targetBody: unknown, credentialsBody: unknown, command: string): Promise<ExecResult> {
    assertEnabled();
    const target = parseTarget(targetBody, true, false);
    return withConnection(connectConfig(target, credentials(credentialsBody)), (client) => execRemote(client, command));
}

function shellQuote(value: string): string {
    return quoteShellArguments([value]);
}

function credentials(body: unknown): RemoteCredentials {
    return (body && typeof body === 'object' ? body : {}) as RemoteCredentials;
}

function parseNamespace(value: unknown): string {
    const namespace = String(value || '').trim();
    if (!NAMESPACE_RE.test(namespace)) throw Object.assign(new Error('Invalid namespace'), { status: 400 });
    return namespace;
}

function combined(result: ExecResult): string {
    return `${result.stdout}${result.stderr}`.trim();
}

export function guideFor(plan: Record<string, unknown>): string {
    const guide = String(plan.guide || '').trim();
    if (guide) {
        if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(guide)) {
            throw Object.assign(new Error('Invalid deployment guide'), { status: 400 });
        }
        return guide;
    }
    const source = String(plan.source || '');
    const prefillReplicas = Number(plan.prefillReplicas || 0);
    if (source === 'tiered-cache') return 'tiered-prefix-cache';
    if (source === 'precise-prefix-cache') return 'precise-prefix-cache-routing';
    if (source === 'epd') throw Object.assign(new Error('E/P/D deployment is not supported by the installed OptimalBench CLI'), { status: 400 });
    return prefillReplicas > 0 ? 'pd-disaggregation' : 'optimized-baseline';
}

function targetValue(plan: Record<string, unknown>, name: string): string {
    const target = plan.deploymentTarget;
    return target && typeof target === 'object' ? String((target as Record<string, unknown>)[name] || '').trim() : '';
}

export function prepareRepositoryCommand(plan: Record<string, unknown>): { command: string; rootVariable: string } {
    const repository = targetValue(plan, 'repository');
    const branch = targetValue(plan, 'branch') || 'main';
    if (!repository) throw Object.assign(new Error('Deployment repository path or GitHub URL is required'), { status: 400 });
    if (!BRANCH_RE.test(branch)) throw Object.assign(new Error('Invalid repository branch'), { status: 400 });

    const isGitHubUrl = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\.git)?\/?$/.test(repository);
    const localPath = repository.startsWith('/') || repository.startsWith('~/');
    if (!isGitHubUrl && !localPath) {
        throw Object.assign(new Error('Repository must be an absolute local path or a GitHub HTTPS URL'), { status: 400 });
    }
    if (localPath) {
        const expandedPath = repository.startsWith('~/')
            ? `"$HOME"/${shellQuote(repository.slice(2))}` : shellQuote(repository);
        return {
            command: `LLM_D_ROOT=${expandedPath}; test -d "$LLM_D_ROOT/.git"; git -C "$LLM_D_ROOT" fetch origin --prune; git -C "$LLM_D_ROOT" checkout ${shellQuote(branch)}`,
            rootVariable: '"$LLM_D_ROOT"',
        };
    }

    const [owner, rawName] = repository.replace(/\/$/, '').split('/').slice(-2);
    const repositoryName = rawName.replace(/\.git$/, '');
    if ([owner, repositoryName].some(value => !value || value === '.' || value === '..')) {
        throw Object.assign(new Error('Invalid GitHub repository path'), { status: 400 });
    }
    const relativeCache = shellQuote(`deploy-repos/${owner}/${repositoryName}`);
    return {
        command: `LENS_DEPLOY_CACHE="\${LENS_CACHE_DIR:-\${XDG_CACHE_HOME:-$HOME/.cache}/lens}"; LLM_D_ROOT="$LENS_DEPLOY_CACHE"/${relativeCache}; mkdir -p "$(dirname "$LLM_D_ROOT")"; if test -d "$LLM_D_ROOT/.git"; then git -C "$LLM_D_ROOT" fetch origin --prune; else git clone ${shellQuote(repository)} "$LLM_D_ROOT"; fi; git -C "$LLM_D_ROOT" checkout ${shellQuote(branch)}`,
        rootVariable: '"$LLM_D_ROOT"',
    };
}

function modelserverPatchCommand(
    namespace: string,
    mountPath: string,
    environment: Array<{ name: string; value: string }>,
): string {
    const encoded = Buffer.from(JSON.stringify({ mountPath, environment })).toString('base64');
    const script = [
        'import base64,json,subprocess,sys',
        'cfg=json.loads(base64.b64decode(sys.argv[1]))',
        'names=[]',
        'for _ in range(60):',
        ' try: names=subprocess.check_output(["kubectl","get","deployment","-n",sys.argv[2],"-o","name"],text=True).splitlines()',
        ' except subprocess.CalledProcessError: names=[]',
        ' if names: break',
        ' import time; time.sleep(1)',
        'if not names: raise SystemExit("modelserver deployment was not created within 60 seconds")',
        'for deployment in names:',
        ' data=json.loads(subprocess.check_output(["kubectl","get",deployment,"-n",sys.argv[2],"-o","json"],text=True))',
        ' containers=data["spec"]["template"]["spec"].get("containers",[])',
        ' container=next((item for item in containers if item.get("name") in ("modelserver","vllm")),None)',
        ' if not container: continue',
        ' patch={"spec":{"template":{"spec":{"containers":[{"name":container["name"],"env":cfg["environment"]}]}}}}',
        ' if cfg["mountPath"]:',
        '  patch["spec"]["template"]["spec"]["volumes"]=[{"name":"model-cache-hostpath","hostPath":{"path":cfg["mountPath"],"type":"DirectoryOrCreate"}}]',
        '  patch["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]=[{"name":"model-cache-hostpath","mountPath":"/model-cache","readOnly":True}]',
        '  patch["spec"]["template"]["spec"]["containers"][0]["env"].append({"name":"HF_HOME","value":"/model-cache"})',
        ' subprocess.check_call(["kubectl","patch",deployment,"-n",sys.argv[2],"--type","strategic","--patch",json.dumps(patch)])',
    ].join('\n');
    return `python3 -c ${shellQuote(script)} ${shellQuote(encoded)} ${shellQuote(namespace)}`;
}

function deploymentCommand(plan: Record<string, unknown>, target: RemoteTarget, namespace: string, name: string): string {
    const accelerator = String(plan.accelerator || '');
    if (!/^Intel Data Center GPU/i.test(accelerator)) {
        throw Object.assign(new Error('The installed OptimalBench deploy backend currently supports Intel Data Center GPU/XPU only'), { status: 400 });
    }
    if (Number(plan.nodes || 1) !== 1) {
        throw Object.assign(new Error('Remote SSH deployment currently supports one Kubernetes control-plane machine only'), { status: 400 });
    }
    const prefillTp = Number(plan.prefillTp || 0);
    const prefillReplicas = Number(plan.prefillReplicas || 0);
    const decodeTp = Number(plan.decodeTp || 1);
    const decodeReplicas = Number(plan.decodeReplicas || plan.replicas || 1);
    for (const [label, value] of Object.entries({ prefillTp, prefillReplicas, decodeTp, decodeReplicas })) {
        if (!Number.isInteger(value) || value < 0 || value > 128 || (label.startsWith('decode') && value < 1)) {
            throw Object.assign(new Error(`Invalid ${label}`), { status: 400 });
        }
    }
    const requiredCards = prefillTp * prefillReplicas + decodeTp * decodeReplicas;
    if (requiredCards > target.cards.length) {
        throw Object.assign(new Error(`Topology requires ${requiredCards} cards but only ${target.cards.length} card IDs were selected`), { status: 400 });
    }
    const model = String(plan.model || '').trim();
    const mountPath = targetValue(plan, 'mountPath');
    const mountModelName = targetValue(plan, 'mountModelName');
    if (Boolean(mountPath) !== Boolean(mountModelName)) {
        throw Object.assign(new Error('Model cache host path and mounted model name must be provided together'), { status: 400 });
    }
    const environment = [
        ['HTTP_PROXY', targetValue(plan, 'httpProxy')],
        ['HTTPS_PROXY', targetValue(plan, 'httpsProxy')],
        ['NO_PROXY', targetValue(plan, 'noProxy')],
    ].filter(([, value]) => value).map(([name, value]) => ({ name, value }));
    const repository = prepareRepositoryCommand(plan);
    const args = [
        shellQuote(DEFAULT_DEPLOY_BIN), 'deploy', 'custom', shellQuote(guideFor(plan)),
        '-e', 'xpu', '-n', shellQuote(namespace), '--release-postfix', shellQuote(name), '--llm-d-root', repository.rootVariable, '--no-wait',
        '--prefill-replicas', String(prefillReplicas), '--prefill-tp', String(Math.max(1, prefillTp)),
        '--decode-replicas', String(decodeReplicas), '--decode-tp', String(decodeTp),
    ];
    const effectiveModel = mountPath ? `/model-cache/${mountModelName}` : model;
    if (effectiveModel) args.push('--model', shellQuote(mountPath || effectiveModel.startsWith('hf://') ? effectiveModel : `hf://${effectiveModel}`));
    if (mountPath) {
        if (!/^\//.test(mountPath) || mountPath.includes('\n') || mountModelName.includes('\n')) {
            throw Object.assign(new Error('Model cache host path must be an absolute path and mounted model name must be one line'), { status: 400 });
        }
    }
    const hostEnvironment = environment.map((item) => `${item.name}=${shellQuote(item.value)}`).join(' ');
    return `${repository.command}; ${hostEnvironment} ${args.join(' ')}; ${modelserverPatchCommand(namespace, mountPath, environment)}`;
}

type KubernetesPod = {
    metadata?: { name?: string };
    spec?: { nodeName?: string; containers?: Array<{ resources?: { limits?: Record<string, string | number> } }> };
    status?: {
        phase?: string;
        conditions?: Array<{ type?: string; status?: string }>;
        containerStatuses?: Array<{ restartCount?: number; state?: { waiting?: { reason?: string; message?: string }; terminated?: { reason?: string; message?: string } } }>;
    };
};

function parseJson<T>(result: ExecResult, label: string): T {
    if (result.code !== 0) throw new Error(`${label}: ${combined(result)}`);
    try {
        return JSON.parse(result.stdout) as T;
    } catch {
        throw new Error(`${label} returned invalid JSON`);
    }
}

async function remoteStatus(client: Client, namespace: string) {
    const podsResult = await execRemote(client, `kubectl get pods -n ${shellQuote(namespace)} -o json`);
    if (podsResult.code !== 0 && /not found/i.test(combined(podsResult))) {
        return { status: 'not_found', pods: [], podsReady: 0, podsTotal: 0, endpoint: null, error: combined(podsResult), logTail: [] };
    }
    const podList = parseJson<{ items?: KubernetesPod[] }>(podsResult, 'kubectl get pods');
    const pods = (podList.items || []).map((pod) => {
        const statuses = pod.status?.containerStatuses || [];
        const reason = statuses.map((item) => item.state?.waiting?.reason || item.state?.terminated?.reason).find(Boolean) || null;
        const message = statuses.map((item) => item.state?.waiting?.message || item.state?.terminated?.message).find(Boolean) || null;
        const gpuCount = (pod.spec?.containers || []).reduce((sum, container) => {
            const limits = container.resources?.limits || {};
            return sum + Number(limits['gpu.intel.com/i915'] || limits['gpu.intel.com/xe'] || 0);
        }, 0);
        return {
            name: pod.metadata?.name || 'unknown',
            status: pod.status?.phase || 'Unknown',
            ready: pod.status?.conditions?.some((condition) => condition.type === 'Ready' && condition.status === 'True') || false,
            restarts: statuses.reduce((sum, item) => sum + Number(item.restartCount || 0), 0),
            node: pod.spec?.nodeName || null,
            gpuCount,
            reason,
            message,
        };
    });
    const failedPod = pods.find((pod) => pod.status === 'Failed' || (pod.reason && TERMINAL_REASONS.has(pod.reason)));
    const podsReady = pods.filter((pod) => pod.ready).length;
    let status = failedPod ? 'failed' : pods.length > 0 && podsReady === pods.length ? 'ready' : 'deploying';
    const gatewayResult = await execRemote(client, `kubectl get gateways.gateway.networking.k8s.io -n ${shellQuote(namespace)} -o json 2>/dev/null || printf '{"items":[]}'`);
    const serviceResult = await execRemote(client, `kubectl get services -n ${shellQuote(namespace)} -o json`);
    const gateways = parseJson<{ items?: Array<{ status?: { addresses?: Array<{ value?: string }> } }> }>(gatewayResult, 'kubectl get gateways');
    const services = parseJson<{ items?: Array<{ metadata?: { name?: string; labels?: Record<string, string> }; spec?: { type?: string; clusterIP?: string; ports?: Array<{ port?: number }> }; status?: { loadBalancer?: { ingress?: Array<{ ip?: string; hostname?: string }> } } }> }>(serviceResult, 'kubectl get services');
    const gatewayAddress = gateways.items?.flatMap((item) => item.status?.addresses || []).find((address) => address.value)?.value;
    const loadBalancer = services.items?.find((item) => item.spec?.type === 'LoadBalancer');
    const ingress = loadBalancer?.status?.loadBalancer?.ingress?.[0];
    const endpointHost = gatewayAddress || ingress?.ip || ingress?.hostname;
    const endpointPort = loadBalancer?.spec?.ports?.[0]?.port;
    const gatewayService = services.items?.find((item) => item.metadata?.labels?.['gateway.networking.k8s.io/gateway-name']
        || item.metadata?.name?.toLowerCase().includes('gateway-istio'));
    const endpoint = endpointHost
        ? `http://${endpointHost}${endpointPort && endpointPort !== 80 ? `:${endpointPort}` : ''}`
        : gatewayService?.metadata?.name
            ? `http://${gatewayService.metadata.name}.${namespace}.svc.cluster.local:${gatewayService.spec?.ports?.[0]?.port || 80}`
            : null;
    if (status === 'ready' && !endpoint) status = 'ready_no_endpoint';
    return {
        status,
        pods,
        podsReady,
        podsTotal: pods.length,
        endpoint,
        error: failedPod ? `${failedPod.name}: ${failedPod.reason || failedPod.status}${failedPod.message ? ` — ${failedPod.message}` : ''}` : null,
        logTail: [],
    };
}

function errorResponse(res: express.Response, error: unknown) {
    const status = typeof error === 'object' && error && 'status' in error ? Number(error.status) : 502;
    const message = error instanceof Error ? error.message : String(error);
    res.status(status).json({ error: message.replace(/password=[^\s]+/gi, 'password=[redacted]') });
}

// Retrieves the host key without authenticating. The UI must show it to the user
// and send it back on authenticated calls, preventing silent man-in-the-middle use.
remoteDeployRouter.get('/api/remote-deploy/defaults', (_req, res) => {
    res.json(remoteTargetDefaults());
});

remoteDeployRouter.post('/api/remote-deploy/fingerprint', (req, res) => {
    try {
        assertEnabled();
        const target = parseTarget({ ...req.body?.target, username: req.body?.target?.username || 'probe', cards: ['0'] }, false);
        const client = new Client();
        let sent = false;
        client.once('error', (error) => {
            if (!sent) errorResponse(res, error);
        });
        client.connect({
            host: target.host,
            port: target.port,
            username: target.username,
            readyTimeout: 10_000,
            hostVerifier: (key) => {
                sent = true;
                res.json({ fingerprint: fingerprint(key), host: target.host, port: target.port });
                client.end();
                return false;
            },
        });
    } catch (error) {
        errorResponse(res, error);
    }
});

remoteDeployRouter.post('/api/remote-deploy/test', async (req, res) => {
    try {
        assertEnabled();
        const target = parseTarget(req.body?.target);
        const requestCredentials = credentials(req.body?.credentials);
        const result = await withConnection(connectConfig(target, requestCredentials), (client) => execRemote(
            client,
            'set -o pipefail; printf "host="; hostname; printf "os="; uname -srm; command -v llm-d-bench; llm-d-bench --version 2>/dev/null || true; kubectl version --client; kubectl cluster-info; printf "cards="; (command -v xpu-smi >/dev/null && xpu-smi discovery -d 2>/dev/null | head -40) || echo "xpu-smi not found"',
        ));
        res.json({ ok: result.code === 0, output: `${result.stdout}${result.stderr}`.trim().slice(-12_000) });
    } catch (error) {
        errorResponse(res, error);
    }
});

remoteDeployRouter.post('/api/remote-deploy/start', async (req, res) => {
    try {
        assertEnabled();
        const target = parseTarget(req.body?.target);
        const requestCredentials = credentials(req.body?.credentials);
        const plan = (req.body?.plan || {}) as Record<string, unknown>;
        const namespace = String(plan.namespace || 'llm-d-optimized').trim();
        const name = String(plan.name || 'llm-d-service').trim();
        if (!NAMESPACE_RE.test(namespace) || !NAMESPACE_RE.test(name)) {
            throw Object.assign(new Error('Invalid service name or namespace'), { status: 400 });
        }
        const command = deploymentCommand(plan, target, namespace, name);
        const result = await withConnection(connectConfig(target, requestCredentials), (client) => execRemote(client, command));
        if (result.code !== 0) {
            throw Object.assign(new Error(`Remote deploy exited with code ${result.code}: ${result.stderr || result.stdout}`), { status: 502 });
        }
        res.json({
            id: `remote-${Date.now()}`,
            name,
            candidateId: plan.candidateId || null,
            candidateName: plan.candidateName || 'Standalone manual configuration',
            cluster: `${target.username}@${target.host}:${target.port}`,
            remoteHost: target.host,
            machineType: plan.machineType,
            accelerator: plan.accelerator,
            nodes: 1,
            gpusPerNode: target.cards.length,
            selectedCards: target.cards,
            replicas: Number(plan.replicas || 1),
            namespace,
            status: 'deploying',
            createdAt: new Date().toISOString(),
            endpoint: null,
            pods: [],
            logTail: `${result.stdout}${result.stderr}`.trim().split(/\r?\n/).slice(-100),
        });
    } catch (error) {
        errorResponse(res, error);
    }
});

remoteDeployRouter.post('/api/remote-deploy/status', async (req, res) => {
    try {
        assertEnabled();
        const target = parseTarget(req.body?.target);
        const namespace = parseNamespace(req.body?.namespace);
        const result = await withConnection(connectConfig(target, credentials(req.body?.credentials)), (client) => remoteStatus(client, namespace));
        res.json(result);
    } catch (error) {
        errorResponse(res, error);
    }
});

remoteDeployRouter.post('/api/remote-deploy/logs', async (req, res) => {
    try {
        assertEnabled();
        const target = parseTarget(req.body?.target);
        const namespace = parseNamespace(req.body?.namespace);
        const pod = String(req.body?.pod || '').trim();
        const tail = Math.min(1000, Math.max(1, Number(req.body?.tail || 100)));
        if (pod && !POD_RE.test(pod)) throw Object.assign(new Error('Invalid pod name'), { status: 400 });
        const command = `${shellQuote(DEFAULT_DEPLOY_BIN)} deploy logs -n ${shellQuote(namespace)}${pod ? ` -p ${shellQuote(pod)}` : ''} --tail ${tail}`;
        const result = await withConnection(connectConfig(target, credentials(req.body?.credentials)), (client) => execRemote(client, command));
        if (result.code !== 0) throw new Error(combined(result));
        res.json({ logs: result.stdout.slice(-32_000), truncated: result.stdout.length >= 32_000 });
    } catch (error) {
        errorResponse(res, error);
    }
});

remoteDeployRouter.post('/api/remote-deploy/teardown', async (req, res) => {
    try {
        assertEnabled();
        const target = parseTarget(req.body?.target);
        const namespace = parseNamespace(req.body?.namespace);
        const command = `${shellQuote(DEFAULT_DEPLOY_BIN)} deploy cleanup -n ${shellQuote(namespace)} -f`;
        const result = await withConnection(connectConfig(target, credentials(req.body?.credentials)), (client) => execRemote(client, command));
        if (result.code !== 0) throw new Error(combined(result));
        res.json({ success: true, namespace, logTail: combined(result).split(/\r?\n/).slice(-100) });
    } catch (error) {
        errorResponse(res, error);
    }
});
