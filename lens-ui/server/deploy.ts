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
// Deploy PoC adapter (Phase 1 vertical slice).
//
// A THIN adapter that lets Prism trigger a REAL llm-d deployment of a single
// known-good config, read its status, and tear it down — without Prism itself
// knowing about Helm/kubectl. It reuses the EXISTING OptimalBench capability:
//   - deploy / teardown  -> the `llm-d-bench` CLI (DeploymentManager)
//   - status             -> the OptimalBench REST service (/api/deployments/..)
//
// Scope is deliberately narrow: 1 known-good config, 1 accelerator, deploy /
// status / teardown only. No recommendation, agent, search, TCO, approval.
// -----------------------------------------------------------------------------

import express from 'express';
import { spawn, type ChildProcessWithoutNullStreams } from 'child_process';
import { quote as quoteShellArguments } from 'shell-quote';
import { clusterSessionKubeconfig } from './clusterSession.ts';

export const deployRouter = express.Router();

const HOME = process.env.HOME || '';

// Adapter configuration (overridable via env).
const OB_BIN = process.env.OPTIMALBENCH_BIN || 'llm-d-bench';
const OB_API = (process.env.OPTIMALBENCH_API || 'http://127.0.0.1:8080').replace(/\/$/, '');
const KUBECONFIG = process.env.OPTIMALBENCH_KUBECONFIG || `${HOME}/.kube/config`;
const LLM_D_ROOT = process.env.LLM_D_ROOT || '';
const CLUSTER_NO_PROXY = process.env.PRISM_CLUSTER_NO_PROXY || 'localhost,127.0.0.1,.svc,.cluster.local';

// The single known-good config this PoC deploys.
const KNOWN_GOOD = {
    id: 'optimized-baseline-xpu',
    label: 'Optimized Baseline · Intel XPU (Qwen3-0.6B)',
    guide: 'optimized-baseline',
    accelerator: 'xpu',
    model: 'Qwen/Qwen3-0.6B',
    defaultNamespace: 'prism-deploy-poc',
};

// A tiny smoke config: proves the full deploy->status->Running->teardown chain
// end-to-end in seconds using the node-cached pause image (no 10GB pull), and
// tolerates disk-pressure so it schedules even on a full node.
const SMOKE = {
    id: 'smoke-pause',
    label: 'Smoke Test · pause (tiny, no image pull)',
    image: 'registry.k8s.io/pause:3.10.1',
    defaultNamespace: 'prism-smoke-test',
};
const KUBECTL = process.env.KUBECTL_BIN || 'kubectl';
const TARGET_NODE = String(process.env.PRISM_K8S_TARGET_NODE || '').trim();

function smokeManifest(namespace: string): string {
    // A bare Pod (no controller): if the node evicts it under disk-pressure it is
    // NOT recreated, so a full node cannot trigger a pod storm. The llm-d.ai/role
    // label lets the OptimalBench status proxy report it.
    return `apiVersion: v1
kind: Namespace
metadata:
  name: ${namespace}
---
apiVersion: v1
kind: Pod
metadata:
  name: llm-d-smoke
  namespace: ${namespace}
  labels: { app: llm-d-smoke, llm-d.ai/role: smoke }
spec:
  restartPolicy: Never
${TARGET_NODE ? `  nodeSelector:
        kubernetes.io/hostname: ${TARGET_NODE}
` : ''}  priorityClassName: system-node-critical
  tolerations:
    - key: node.kubernetes.io/disk-pressure
      operator: Exists
      effect: NoSchedule
  containers:
    - name: pause
      image: ${SMOKE.image}
      resources:
        requests: { cpu: "10m", memory: "16Mi" }
        limits: { cpu: "50m", memory: "32Mi" }
`;
}

// In-memory record of the last deploy/teardown launched per namespace. The live
// K8s truth still comes from the status proxy; this only tracks the CLI job.
type Job = {
    action: 'deploy' | 'teardown';
    phase: 'running' | 'succeeded' | 'failed';
    startedAt: string;
    finishedAt?: string;
    exitCode?: number;
    logTail: string[];
};
const jobs = new Map<string, Job>();

function childEnv(sessionId?: unknown): NodeJS.ProcessEnv {
    const inheritedEnv = { ...process.env };
    delete inheritedEnv.LLM_D_ROOT;
    const existingNoProxy = process.env.NO_PROXY || process.env.no_proxy || '';
    const noProxy = [existingNoProxy, CLUSTER_NO_PROXY].filter(Boolean).join(',');
    const sessionKubeconfig = sessionId ? clusterSessionKubeconfig(sessionId) : null;
    return {
        ...inheritedEnv,
        // A supplied session must never fall back to the BFF host's default cluster.
        KUBECONFIG: sessionId ? sessionKubeconfig || '/dev/null' : KUBECONFIG,
        NO_PROXY: noProxy,
        no_proxy: noProxy,
    };
}

function pushLog(job: Job, chunk: string) {
    for (const line of chunk.split(/\r?\n/)) {
        if (line.trim()) job.logTail.push(line);
    }
    // Keep only the last 200 lines.
    if (job.logTail.length > 200) job.logTail.splice(0, job.logTail.length - 200);
}

function trackChild(namespace: string, action: 'deploy' | 'teardown', child: ChildProcessWithoutNullStreams, stdin?: string): Job {
    const job: Job = { action, phase: 'running', startedAt: new Date().toISOString(), logTail: [] };
    jobs.set(namespace, job);

    child.stdout.on('data', (d) => pushLog(job, d.toString()));
    child.stderr.on('data', (d) => pushLog(job, d.toString()));
    child.on('error', (err) => {
        job.phase = 'failed';
        job.finishedAt = new Date().toISOString();
        pushLog(job, `spawn error: ${err.message}`);
    });
    child.on('close', (code) => {
        job.exitCode = code ?? -1;
        job.phase = code === 0 ? 'succeeded' : 'failed';
        job.finishedAt = new Date().toISOString();
    });
    if (stdin !== undefined) {
        child.stdin.write(stdin);
        child.stdin.end();
    }
    return job;
}

function runProc(namespace: string, action: 'deploy' | 'teardown', bin: string, args: string[], stdin?: string, sessionId?: unknown): Job {
    return trackChild(namespace, action, spawn(bin, args, { env: childEnv(sessionId) }), stdin);
}

function runShellCommand(namespace: string, action: 'deploy' | 'teardown', command: string, sessionId?: unknown): Job {
    return trackChild(namespace, action, spawn('bash', ['-c', command], { env: childEnv(sessionId) }));
}

function runCli(namespace: string, action: 'deploy' | 'teardown', args: string[], sessionId?: unknown): Job {
    return runProc(namespace, action, OB_BIN, args, undefined, sessionId);
}

function shellQuote(value: string): string {
    return quoteShellArguments([value]);
}

export function buildCliDeployCommand(kubectl: string, namespace: string, optimalBenchBin: string, llmDRoot: string): string {
    return `${shellQuote(kubectl)} get namespace ${shellQuote(namespace)} >/dev/null 2>&1 || `
        + `${shellQuote(kubectl)} create namespace ${shellQuote(namespace)} ; `
        + `exec ${shellQuote(optimalBenchBin)} deploy guide ${KNOWN_GOOD.guide} -e ${KNOWN_GOOD.accelerator} `
        + `-n ${shellQuote(namespace)} --llm-d-root ${shellQuote(llmDRoot)} --no-wait`;
}

// GET the known-good config descriptors for the Prism deploy dropdown.
deployRouter.get('/api/deploy-poc/config', (_req, res) => {
    res.json({ configs: [SMOKE, KNOWN_GOOD] });
});

// POST start a deployment. config='smoke-pause' runs a tiny kubectl deploy;
// otherwise the llm-d known-good guide via the CLI.
deployRouter.post('/api/deploy-poc/start', (req, res) => {
    const config = (req.body?.config || KNOWN_GOOD.id).toString();
    const isSmoke = config === SMOKE.id;
    const namespace = (req.body?.namespace || (isSmoke ? SMOKE.defaultNamespace : KNOWN_GOOD.defaultNamespace)).toString().trim();
    if (!/^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/.test(namespace)) {
        return res.status(400).json({ error: 'invalid namespace' });
    }
    const existing = jobs.get(namespace);
    if (existing && existing.action === 'deploy' && existing.phase === 'running') {
        return res.status(409).json({ error: 'deploy already running', namespace });
    }
    const sessionId = req.body?.clusterSessionId;
    if (sessionId && !clusterSessionKubeconfig(sessionId)) return res.status(409).json({ error: 'cluster session is no longer active' });
    if (isSmoke) {
        runProc(namespace, 'deploy', KUBECTL, ['apply', '-f', '-'], smokeManifest(namespace), sessionId);
        return res.json({ namespace, config: SMOKE.id, phase: 'deploying' });
    }
    if (TARGET_NODE) {
        return res.status(409).json({
            error: 'legacy PoC deployment cannot enforce PRISM_K8S_TARGET_NODE; use the Deploy runtime',
            targetNode: TARGET_NODE,
        });
    }
    // Pre-create the namespace so the CLI's HF-secret step doesn't warn about a
    // missing namespace (the guide would create it later anyway). Qwen3-0.6B is
    // public, so the HF token is optional. --no-wait: return fast; Prism polls.
    const cliCmd = buildCliDeployCommand(KUBECTL, namespace, OB_BIN, LLM_D_ROOT);
    runShellCommand(namespace, 'deploy', cliCmd, sessionId);
    res.json({ namespace, config: KNOWN_GOOD.id, phase: 'deploying' });
});

// GET live status: merge the CLI job phase with real K8s pod status from the
// OptimalBench REST service.
deployRouter.get('/api/deploy-poc/status', async (req, res) => {
    const namespace = (req.query.namespace || KNOWN_GOOD.defaultNamespace).toString().trim();
    const job = jobs.get(namespace) || null;
    let k8s: unknown = null;
    let k8sError: string | null = null;
    try {
        const r = await fetch(`${OB_API}/api/deployments/${encodeURIComponent(namespace)}/status`);
        if (r.ok) k8s = await r.json();
        else k8sError = `status ${r.status}`;
    } catch (e) {
        k8sError = e instanceof Error ? e.message : String(e);
    }
    res.json({
        namespace,
        job: job
            ? { action: job.action, phase: job.phase, exitCode: job.exitCode, startedAt: job.startedAt, finishedAt: job.finishedAt, logTail: job.logTail.slice(-100) }
            : null,
        k8s,
        k8sError,
    });
});

// GET post-deploy validation (D8, Layer 1): real readiness checks against the
// live cluster status. Layer 2 (HTTP smoketest against the model endpoint)
// requires a reachable serving endpoint and is added once the node can run it.
deployRouter.get('/api/deploy-poc/validate', async (req, res) => {
    const namespace = (req.query.namespace || KNOWN_GOOD.defaultNamespace).toString().trim();
    let k8s: {
        status?: string; pods_total?: number; pods_ready?: number; gateway_endpoint?: string | null;
    } | null = null;
    let error: string | null = null;
    try {
        const r = await fetch(`${OB_API}/api/deployments/${encodeURIComponent(namespace)}/status`);
        if (r.ok) k8s = await r.json();
        else error = `status ${r.status}`;
    } catch (e) {
        error = e instanceof Error ? e.message : String(e);
    }
    const k = k8s || {};
    const total = k.pods_total || 0;
    const ready = k.pods_ready || 0;
    const checks = [
        { name: 'Namespace exists', pass: !!k.status && k.status !== 'not_found' },
        { name: 'Pods scheduled', pass: total > 0 },
        { name: 'All pods Ready', pass: total > 0 && ready === total },
        { name: 'Gateway endpoint present', pass: !!k.gateway_endpoint },
    ];
    const passed = checks.filter((c) => c.pass).length;
    res.json({
        namespace,
        ready: total > 0 && ready === total,
        checks,
        passed,
        total: checks.length,
        endpoint: k.gateway_endpoint || null,
        error,
    });
});

// POST teardown: delete the deployment + namespace so the PoC is repeatable.
// config='smoke-pause' deletes the namespace via kubectl; otherwise CLI destroy.
deployRouter.post('/api/deploy-poc/teardown', (req, res) => {
    const config = (req.body?.config || KNOWN_GOOD.id).toString();
    const isSmoke = config === SMOKE.id;
    const namespace = (req.body?.namespace || (isSmoke ? SMOKE.defaultNamespace : KNOWN_GOOD.defaultNamespace)).toString().trim();
    const sessionId = req.body?.clusterSessionId;
    if (!/^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/.test(namespace)) {
        return res.status(400).json({ error: 'invalid namespace' });
    }
    if (isSmoke) {
        runProc(namespace, 'teardown', KUBECTL, ['delete', 'namespace', namespace, '--ignore-not-found', '--wait=false'], undefined, sessionId);
        return res.json({ namespace, phase: 'deleting' });
    }
    const args = [
        'deploy', 'destroy', KNOWN_GOOD.guide,
        '-e', KNOWN_GOOD.accelerator,
        '-n', namespace,
        `--llm-d-root=${LLM_D_ROOT}`,
        '--delete-namespace',
    ];
    runCli(namespace, 'teardown', args, sessionId);
    res.json({ namespace, phase: 'deleting' });
});
