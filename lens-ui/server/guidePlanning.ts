// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import express from 'express';
import yaml from 'js-yaml';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import crypto from 'node:crypto';
import { execFile, execFileSync } from 'node:child_process';
import { promisify } from 'node:util';
import { readGuideSource } from './guideSourceFetch.ts';
import { inspectPlanningCluster } from './planningDiscovery.ts';
import { performance } from 'node:perf_hooks';
import { storagePath } from './storagePaths.ts';
import { createGuideManifestCache } from './guideManifestCache.ts';
import { loadClusterSources } from './clusterSources.ts';
import { clusterSessionKubeconfig } from './clusterSession.ts';
import { executeRemoteInspection } from './remoteDeploy.ts';
import { applyRuntimeOverrides, runtimeOverrides } from './configurationOverrides.ts';
import { configureModelServer, configureCpuCache, runtimeArgument } from './modelServerConfiguration.ts';
import { normalizeGuideSettings } from '../src/features/evaluation/guideSettings.js';
import { buildGuideDeploymentBundle, pinModelServerImage, ROUTER_DISAGG_SIDECAR_IMAGE } from './guideDeploymentBundle.ts';

/* YAML manifests and Kubernetes discovery responses are intentionally schema-dynamic. */
/* eslint-disable @typescript-eslint/no-explicit-any */

export const guidePlanningRouter = express.Router();

const GITHUB_REPOSITORY = process.env.LLM_D_GUIDES_REPOSITORY || 'llm-d/llm-d';
const GITHUB_API = 'https://api.github.com';

type JsonRecord = Record<string, any>;
type CatalogEntry = { id: string; label: string; accelerators: Array<{ id: string; modelServers: Array<{ id: string; variants: string[] }> }> };
type ManifestSet = { modelServer?: string; localRoot?: string; variant: string; files: Array<{ path: string; content: string }>; rendered: string; source: JsonRecord };

function setEnvironmentValue(environment: JsonRecord[], name: string, value: string) {
    const existing = environment.find((item) => item?.name === name);
    if (existing) existing.value = value;
    else environment.push({ name, value });
}

const execFileAsync = promisify(execFile);
const GUIDE_CACHE_ROOT = storagePath('cache', ['guide-planning']);
const loadCachedManifest = createGuideManifestCache<ManifestSet>(24, {
    directory: path.join(GUIDE_CACHE_ROOT, 'manifests'),
    validate: (value: unknown): value is ManifestSet => {
        const item = value as ManifestSet | null;
        return Boolean(item && typeof item.rendered === 'string' && item.rendered.trim() && Array.isArray(item.files) && /^[0-9a-f]{40}$/.test(String(item.source?.commit || '')));
    },
});
function githubHeaders(): Record<string, string> {
    const headers: Record<string, string> = { Accept: 'application/vnd.github+json', 'User-Agent': 'llm-d-prism' };
    if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
    return headers;
}

async function fetchText(url: string, accept = 'application/vnd.github+json'): Promise<string> {
    return readGuideSource(url, { ...githubHeaders(), Accept: accept });
}

function githubContentUrl(repository: string, manifestPath: string, ref: string): string {
    const encodedRepository = repository.split('/').map(encodeURIComponent).join('/');
    return `${GITHUB_API}/repos/${encodedRepository}/contents/${manifestPath.split('/').map(encodeURIComponent).join('/')}?ref=${encodeURIComponent(ref)}`;
}

async function loadGuidePaths(selection: { clusterId?: unknown; clusterSessionId?: unknown }) {
    const cluster = await loadClusterSources(selection);
    const localRoot = cluster.llmDRepoPath!;
    const commit = commandOutput('git', ['-C', localRoot, 'rev-parse', 'HEAD']);
    const paths = commandOutput('git', ['-C', localRoot, 'ls-files', 'guides'])
        .split('\n').filter((item) => /^guides\/[^/]+\/modelserver\/.+\.ya?ml$/i.test(item));
    if (!/^[0-9a-f]{40}$/.test(commit) || !paths.length) {
        throw Object.assign(new Error('The cluster llm-d source has no usable guides; download its Software Versions'), { status: 409 });
    }
    return { paths, commit, localRoot, ref: cluster.llmDRef || '' };
}

function catalogFromPaths(paths: string[]): CatalogEntry[] {
    const guides = new Map<string, Map<string, Map<string, Set<string>>>>();
    for (const manifestPath of paths) {
        const parts = manifestPath.split('/');
        if (parts.length < 5 || parts[0] !== 'guides' || parts[2] !== 'modelserver') continue;
        const [, guide, , accelerator, modelServer, ...rest] = parts;
        const variant = rest.length > 1 ? rest[0] : '.';
        if (!guides.has(guide)) guides.set(guide, new Map());
        const accelerators = guides.get(guide)!;
        if (!accelerators.has(accelerator)) accelerators.set(accelerator, new Map());
        const modelServers = accelerators.get(accelerator)!;
        if (!modelServers.has(modelServer)) modelServers.set(modelServer, new Set());
        modelServers.get(modelServer)!.add(variant);
    }
    return [...guides.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([id, accelerators]) => ({
        id,
        label: id.split('-').map((part) => part[0]?.toUpperCase() + part.slice(1)).join(' '),
        accelerators: [...accelerators.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([accelerator, modelServers]) => ({
            id: accelerator,
            modelServers: [...modelServers.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([modelServer, variants]) => ({
                id: modelServer,
                variants: [...variants].sort((left, right) => (left === 'base' ? -1 : right === 'base' ? 1 : left.localeCompare(right))),
            })),
        })),
    }));
}

async function getCatalog(selection: { clusterId?: unknown; clusterSessionId?: unknown }) {
    const source = await loadGuidePaths(selection);
    return { ...source, catalog: catalogFromPaths(source.paths) };
}

function commandOutput(command: string, args: string[], timeout = 4000): string {
    try {
        return execFileSync(command, args, { encoding: 'utf8', timeout, stdio: ['ignore', 'pipe', 'ignore'] }).trim();
    } catch {
        return '';
    }
}

function parseAccelerators(): JsonRecord {
    const nvidia = commandOutput('nvidia-smi', ['--query-gpu=name,memory.total', '--format=csv,noheader,nounits']);
    if (nvidia) {
        const devices = nvidia.split('\n').filter(Boolean).map((line) => {
            const [model, memoryMiB] = line.split(',').map((value) => value.trim());
            return { model, memoryGiB: Math.round(Number(memoryMiB) / 1024) };
        });
        return { vendor: 'nvidia', model: devices[0]?.model, count: devices.length, memoryGiB: devices[0]?.memoryGiB, devices };
    }
    const rocm = commandOutput('rocm-smi', ['--showproductname', '--showmeminfo', 'vram', '--csv']);
    if (rocm) return { vendor: 'amd', model: 'AMD accelerator', count: Math.max(1, (rocm.match(/^card\d+/gm) || []).length), raw: rocm.slice(0, 2000) };
    const xpu = commandOutput('xpu-smi', ['discovery', '-d']);
    if (xpu) return { vendor: 'intel', model: xpu.match(/Device Name\s*:\s*(.+)/i)?.[1]?.trim() || 'Intel XPU', count: Math.max(1, (xpu.match(/Device ID\s*:/gi) || []).length), raw: xpu.slice(0, 2000) };
    return { vendor: 'none', model: null, count: 0, memoryGiB: null, devices: [] };
}

function machineProfile(inspectAccelerators = true): JsonRecord {
    return {
        hostname: os.hostname(),
        platform: `${os.type()} ${os.release()} ${os.arch()}`,
        cpu: { model: os.cpus()[0]?.model || 'unknown', logicalCores: os.cpus().length },
        memoryGiB: Number((os.totalmem() / 1024 ** 3).toFixed(1)),
        accelerator: inspectAccelerators ? parseAccelerators() : { vendor: 'none', count: 0, devices: [] },
    };
}

function quantityNumber(value: unknown): number {
    const text = String(value || '0');
    const number = Number.parseFloat(text);
    if (!Number.isFinite(number)) return 0;
    if (text.endsWith('Ki')) return number / 1024 ** 2;
    if (text.endsWith('Mi')) return number / 1024;
    if (text.endsWith('Gi')) return number;
    if (text.endsWith('Ti')) return number * 1024;
    if (text.endsWith('m')) return number / 1000;
    return number;
}

function clusterProfile(version: JsonRecord, nodes: JsonRecord, deviceClasses: JsonRecord | null, resourceSlices: JsonRecord | null, runtimeClasses: JsonRecord | null = null): JsonRecord {
    const nodeProfiles = (nodes.items || []).map((node: JsonRecord) => ({
            name: node.metadata?.name,
            labels: node.metadata?.labels || {},
            taints: node.spec?.taints || [],
            allocatable: node.status?.allocatable || {},
    }));
    const totals: JsonRecord = { cpu: 0, memoryGiB: 0, accelerators: {} };
    for (const node of nodeProfiles) {
        totals.cpu += quantityNumber(node.allocatable.cpu);
        totals.memoryGiB += quantityNumber(node.allocatable.memory);
        for (const [resource, value] of Object.entries(node.allocatable)) {
            if (/gpu|gaudi|xpu|tpu/i.test(resource) && !/monitor/i.test(resource)) {
                totals.accelerators[resource] = (totals.accelerators[resource] || 0) + quantityNumber(value);
            }
        }
    }
    const slices = (resourceSlices?.items || []).map((item: JsonRecord) => ({
        name: item.metadata?.name,
        nodeName: item.spec?.nodeName,
        driver: item.spec?.driver,
        devices: item.spec?.devices || [],
    }));
    const draDevices = slices.flatMap((slice: JsonRecord) =>
        (slice.devices || [])
        .filter((device: JsonRecord) => (
            /gpu|xpu/i.test(String(slice.driver || ''))
            || String(device.attributes?.type?.string || '').toLowerCase() === 'gpu'
        ))
        .map((device: JsonRecord) => ({
            name: device.name,
            driver: slice.driver,
            model: device.attributes?.model?.string || device.attributes?.family?.string || null,
            family: device.attributes?.family?.string || null,
            pciAddress: device.attributes?.pciAddress?.string || device.attributes?.['resource.kubernetes.io/pciBusID']?.string || null,
            memoryGiB: quantityNumber(device.capacity?.memory?.value ?? device.capacity?.memory),
            health: device.attributes?.health?.string || null,
        })),
    );
    const firstDevice = draDevices[0];
    const firstDriver = String(firstDevice?.driver || '');
    const accelerator = draDevices.length ? {
        vendor: /intel/i.test(firstDriver) ? 'intel' : /nvidia/i.test(firstDriver) ? 'nvidia' : /amd/i.test(firstDriver) ? 'amd' : firstDriver || 'unknown',
        model: firstDevice.model,
        count: draDevices.length,
        memoryGiB: firstDevice.memoryGiB || null,
        devices: draDevices,
        source: 'kubernetes-dra',
    } : null;
    return {
        version: version.serverVersion?.gitVersion,
        nodes: nodeProfiles,
        allocatable: totals,
        accelerator,
        deviceClasses: (deviceClasses?.items || []).map((item: JsonRecord) => item.metadata?.name),
        runtimeClasses: (runtimeClasses?.items || []).map((item: JsonRecord) => ({ name: item.metadata?.name, handler: item.handler })),
        resourceSlices: slices.map((slice: JsonRecord) => ({ name: slice.name, nodeName: slice.nodeName, driver: slice.driver, deviceCount: slice.devices.length })),
        preflightPassed: true,
    };
}

// A vendor's container runtime class (nvidia/intel) when the cluster defines
// one; workloads requesting the accelerator must run under it if the default
// runtime is not the vendor's (otherwise the container cannot see the device).
export function runtimeClassForAccelerator(cluster: JsonRecord | null, accelerator: string): string | null {
    const classes = (cluster?.runtimeClasses || []) as JsonRecord[];
    const needle = accelerator === 'gpu' ? 'nvidia' : accelerator === 'xpu' ? 'intel' : '';
    if (!needle) return null;
    const match = classes.find((item) => `${item?.name || ''} ${item?.handler || ''}`.toLowerCase().includes(needle));
    return match ? String(match.name || '') || null : null;
}

function workloadPodSpec(document: JsonRecord): JsonRecord | null {
    const spec = document?.spec;
    if (!spec) return null;
    if (document.kind === 'LeaderWorkerSet') return spec.leaderWorkerTemplate?.workerTemplate?.spec || null;
    if (['Deployment', 'StatefulSet', 'DaemonSet', 'ReplicaSet', 'Job'].includes(String(document.kind))) return spec.template?.spec || null;
    return null;
}

async function discoverLocalCluster(kubeconfig: string | null = null): Promise<JsonRecord | null> {
    const inspected = await inspectPlanningCluster(kubeconfig);
    return inspected ? clusterProfile(inspected.version, inspected.nodes, inspected.deviceClasses, inspected.resourceSlices, inspected.runtimeClasses) : null;
}

function inspectionSections(output: string): Record<string, string> {
    const sections: Record<string, string> = {};
    const marker = /^__PRISM_([A-Z_]+)__$/gm;
    const matches = [...output.matchAll(marker)];
    matches.forEach((match, index) => {
        const start = (match.index || 0) + match[0].length;
        const end = matches[index + 1]?.index ?? output.length;
        sections[match[1]] = output.slice(start, end).trim();
    });
    return sections;
}

function parseJsonSection(sections: Record<string, string>, name: string): JsonRecord | null {
    try { return sections[name] ? JSON.parse(sections[name]) as JsonRecord : null; } catch { return null; }
}

function remoteAccelerators(sections: Record<string, string>): JsonRecord {
    if (sections.NVIDIA) {
        const devices = sections.NVIDIA.split('\n').filter(Boolean).map((line) => {
            const [model, memoryMiB] = line.split(',').map((value) => value.trim());
            return { model, memoryGiB: Math.round(Number(memoryMiB) / 1024) };
        });
        return { vendor: 'nvidia', model: devices[0]?.model, count: devices.length, memoryGiB: devices[0]?.memoryGiB, devices };
    }
    if (sections.ROCM) return { vendor: 'amd', model: 'AMD accelerator', count: Math.max(1, (sections.ROCM.match(/^card\d+/gm) || []).length), raw: sections.ROCM.slice(0, 2000) };
    if (sections.XPU) return { vendor: 'intel', model: sections.XPU.match(/Device Name\s*:\s*(.+)/i)?.[1]?.trim() || 'Intel XPU', count: Math.max(1, (sections.XPU.match(/Device ID\s*:/gi) || []).length), raw: sections.XPU.slice(0, 2000) };
    return { vendor: 'none', model: null, count: 0, memoryGiB: null, devices: [] };
}

async function discoverRemoteEnvironment(target: unknown, requestCredentials: unknown): Promise<{ machine: JsonRecord; cluster: JsonRecord | null }> {
    const command = [
        'printf "__PRISM_HOST__\\n"; hostname',
        'printf "__PRISM_PLATFORM__\\n"; uname -srm',
        'printf "__PRISM_CPU__\\n"; (lscpu -J 2>/dev/null || true)',
        'printf "__PRISM_CORES__\\n"; (getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 0)',
        'printf "__PRISM_MEMORY__\\n"; awk \'/MemTotal/ {print $2}\' /proc/meminfo 2>/dev/null',
        'printf "__PRISM_NVIDIA__\\n"; (nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null || true)',
        'printf "__PRISM_ROCM__\\n"; (rocm-smi --showproductname --showmeminfo vram --csv 2>/dev/null || true)',
        'printf "__PRISM_XPU__\\n"; (xpu-smi discovery -d 2>/dev/null || true)',
        'printf "__PRISM_K8S_VERSION__\\n"; (kubectl version -o json 2>/dev/null || true)',
        'printf "__PRISM_K8S_NODES__\\n"; (kubectl get nodes -o json 2>/dev/null || true)',
        'printf "__PRISM_DEVICE_CLASSES__\\n"; (kubectl get deviceclasses.resource.k8s.io -o json 2>/dev/null || true)',
        'printf "__PRISM_RESOURCE_SLICES__\\n"; (kubectl get resourceslices.resource.k8s.io -o json 2>/dev/null || true)',
    ].join('; ');
    const result = await executeRemoteInspection(target, requestCredentials, command);
    const sections = inspectionSections(result.stdout);
    const cpuJson = parseJsonSection(sections, 'CPU');
    const cpuModel = cpuJson?.lscpu?.find((item: JsonRecord) => item.field === 'Model name:')?.data || 'unknown';
    const version = parseJsonSection(sections, 'K8S_VERSION');
    const nodes = parseJsonSection(sections, 'K8S_NODES');
    const machine = {
        hostname: sections.HOST || 'remote-machine',
        platform: sections.PLATFORM || 'unknown',
        cpu: { model: cpuModel, logicalCores: Number(sections.CORES || 0) },
        memoryGiB: Number((Number(sections.MEMORY || 0) / 1024 ** 2).toFixed(1)),
        accelerator: remoteAccelerators(sections),
        connection: 'ssh',
    };
    const cluster = version && nodes ? clusterProfile(version, nodes, parseJsonSection(sections, 'DEVICE_CLASSES'), parseJsonSection(sections, 'RESOURCE_SLICES')) : null;
    return { machine, cluster };
}

function estimateModel(model: string): JsonRecord {
    const match = model.match(/(?:^|[-_])(\d+(?:\.\d+)?)b(?:[-_]|$)/i);
    const parametersBillions = match ? Number(match[1]) : null;
    return {
        id: model,
        parametersBillions,
        estimatedWeightMemoryGiB: parametersBillions ? Number((parametersBillions * 2 * 1.2 * 1e9 / 1024 ** 3).toFixed(1)) : null,
        assumption: parametersBillions ? 'BF16/FP16 weights plus 20% runtime overhead' : 'Model size could not be inferred from its ID',
    };
}

function knownAcceleratorMemoryGiB(model: unknown): number {
    const name = String(model || '').toLowerCase();
    if (/\bb60\b/.test(name)) return 24;
    if (/max\s*1550|max_1550/.test(name)) return 48;
    if (/max\s*1100|max_1100/.test(name)) return 48;
    return 0;
}

function acceleratorResource(resources: JsonRecord): [string, number] | null {
    for (const [name, value] of Object.entries(resources || {})) {
        if (/gpu|gaudi|xpu|tpu/i.test(name)) return [name, Number(value) || 0];
    }
    return null;
}

function setArgument(args: any[], name: string, value: number): void {
    const flag = `--${name}`;
    const index = args.findIndex((item) => typeof item === 'string' && (item === flag || item.startsWith(`${flag}=`)));
    if (index < 0) {
        args.push(`${flag}=${value}`);
    } else if (args[index] === flag && index + 1 < args.length) {
        args[index + 1] = String(value);
    } else {
        args[index] = `${flag}=${value}`;
    }
}



const PD_ROUTING_PROXY_IMAGE = ROUTER_DISAGG_SIDECAR_IMAGE;

// --- User-supplied custom patches -------------------------------------------------
//
// Some topologies (e.g. a hand-built RDMA dual-rail overlay) are not expressed by any
// override this planner already understands, and are not published as a checked-in Guide
// overlay either. Rather than growing planDocuments' override surface indefinitely, a user
// can attach one or more small Kubernetes-style YAML patch documents to a single plan
// request. Patches are applied only to the in-memory `documents` array produced for that
// one request/response, are never written to disk or cached, and are never merged into the
// Guide catalog. Other guides, other plan requests, and Evaluation runs that do not
// themselves attach customPatches are therefore completely unaffected by this feature.
const MAX_CUSTOM_PATCH_FILES = 20;
const MAX_CUSTOM_PATCH_BYTES = 262_144; // 256 KiB per file, generous for a hand-written overlay

type CustomPatchInput = { path?: unknown; content?: unknown };
export type AppliedCustomPatch = { path: string; kind: string; name: string | null; action: 'merged' | 'appended' };

function isPlainObject(value: unknown): value is JsonRecord {
    return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function mergeArrayByName(target: any[], patch: any[]): any[] {
    const targetHasNames = target.every((item) => isPlainObject(item) && typeof item.name === 'string');
    const patchHasNames = patch.every((item) => isPlainObject(item) && typeof item.name === 'string');
    // Without a shared "name" merge key on both sides (the convention Kubernetes strategic
    // merge patch itself uses for containers/env/volumes/ports/device requests/...) there is
    // no safe way to align entries positionally, so the patch's array simply replaces the
    // guide's array — the same fallback JSON merge patch (RFC 7386) uses for arrays.
    if (!targetHasNames || !patchHasNames) return patch;
    const merged = target.map((item) => ({ ...item }));
    for (const patchItem of patch) {
        const index = merged.findIndex((item) => item.name === patchItem.name);
        if (index >= 0) merged[index] = deepMergePatch(merged[index], patchItem);
        else merged.push(patchItem);
    }
    return merged;
}

function deepMergePatch(target: any, patch: any): any {
    if (Array.isArray(patch)) return Array.isArray(target) ? mergeArrayByName(target, patch) : patch;
    if (isPlainObject(patch)) {
        const base: JsonRecord = isPlainObject(target) ? { ...target } : {};
        for (const [key, value] of Object.entries(patch)) {
            base[key] = key in base ? deepMergePatch(base[key], value) : value;
        }
        return base;
    }
    return patch;
}

function findPatchTargetIndex(documents: any[], patchDocument: JsonRecord, fileLabel: string): number {
    const kind = String(patchDocument.kind || '');
    const name = patchDocument.metadata?.name ? String(patchDocument.metadata.name) : null;
    const candidates = documents
        .map((document, index) => ({ document, index }))
        .filter(({ document }) => isPlainObject(document) && document.kind === kind && (!name || document.metadata?.name === name));
    if (name) return candidates[0]?.index ?? -1;
    if (candidates.length > 1) {
        throw Object.assign(new Error(`Custom patch ${fileLabel} targets kind "${kind}" without metadata.name, and the base manifest has ${candidates.length} matching resources. Add metadata.name to disambiguate.`), { status: 400 });
    }
    return candidates[0]?.index ?? -1;
}

/**
 * Merge user-uploaded patch YAML onto the planned documents for a single plan request.
 * Mutates `documents` in place and returns what was applied so it can be surfaced back to
 * the caller (and, downstream, recorded in the resulting configuration artifact's
 * provenance) for audit/reproducibility. Never touches the shared guide catalog cache or
 * any file on disk, so it cannot leak into other guides or other Evaluation runs.
 */
export function applyCustomPatches(documents: any[], customPatches: CustomPatchInput[], errors: string[], warnings: string[]): AppliedCustomPatch[] {
    const applied: AppliedCustomPatch[] = [];
    const files = Array.isArray(customPatches) ? customPatches : [];
    if (files.length > MAX_CUSTOM_PATCH_FILES) {
        errors.push(`Too many custom patch files (${files.length}); at most ${MAX_CUSTOM_PATCH_FILES} are allowed per plan request.`);
        return applied;
    }
    for (const patchFile of files) {
        const fileLabel = String(patchFile?.path || 'uploaded-patch.yaml').trim() || 'uploaded-patch.yaml';
        const content = String(patchFile?.content || '');
        if (!content.trim()) continue;
        if (Buffer.byteLength(content, 'utf8') > MAX_CUSTOM_PATCH_BYTES) {
            errors.push(`Custom patch ${fileLabel} is larger than the ${MAX_CUSTOM_PATCH_BYTES / 1024} KiB per-file limit.`);
            continue;
        }
        let patchDocuments: any[];
        try {
            patchDocuments = [];
            yaml.loadAll(content, (document) => { if (document) patchDocuments.push(document); });
        } catch (error) {
            errors.push(`Custom patch ${fileLabel} could not be parsed as YAML: ${error instanceof Error ? error.message : String(error)}`);
            continue;
        }
        if (!patchDocuments.length) {
            warnings.push(`Custom patch ${fileLabel} did not contain any YAML documents.`);
            continue;
        }
        for (const patchDocument of patchDocuments) {
            if (!isPlainObject(patchDocument)) {
                errors.push(`Custom patch ${fileLabel} contains a document that is not a YAML mapping.`);
                continue;
            }
            if (!patchDocument.kind) {
                errors.push(`Custom patch ${fileLabel} has a document with no "kind"; it cannot be matched to a base manifest resource.`);
                continue;
            }
            let targetIndex: number;
            try {
                targetIndex = findPatchTargetIndex(documents, patchDocument, fileLabel);
            } catch (error) {
                errors.push(error instanceof Error ? error.message : String(error));
                continue;
            }
            const kind = String(patchDocument.kind);
            const name = patchDocument.metadata?.name ? String(patchDocument.metadata.name) : null;
            if (targetIndex >= 0) {
                documents[targetIndex] = deepMergePatch(documents[targetIndex], patchDocument);
                applied.push({ path: fileLabel, kind, name, action: 'merged' });
            } else {
                documents.push(patchDocument);
                warnings.push(`Custom patch ${fileLabel} (${kind}${name ? `/${name}` : ''}) did not match an existing resource in the base manifest; it was added as a new resource.`);
                applied.push({ path: fileLabel, kind, name, action: 'appended' });
            }
        }
    }
    return applied;
}

/** Count the effective accelerator demand after defaults and custom patches. */
export function validateManifestCapacity(documents: any[], budget: number, basis: string, errors: string[]) {
    const claims = documents.filter(document => ['ResourceClaimTemplate', 'ResourceClaim'].includes(document.kind));
    const demand = documents.filter(document => ['Deployment', 'StatefulSet'].includes(document.kind)).reduce((total, document) => {
        const pod = document.spec?.template?.spec || {};
        const containers = pod.containers || [];
        const containerDemand = (container: JsonRecord) => {
            const resources = { ...container.resources?.limits, ...container.resources?.requests };
            return Object.entries(resources).filter(([name]) => /gpu|gaudi|xpu|tpu/i.test(name)).reduce((sum, [, value]) => sum + Number(value), 0);
        };
        const regularDemand = containers.reduce((sum: number, container: JsonRecord) => sum + containerDemand(container), 0);
        let persistentInit = 0, initPeak = 0;
        for (const container of pod.initContainers || []) {
            const count = containerDemand(container);
            if (container.restartPolicy === 'Always') persistentInit += count;
            initPeak = Math.max(initPeak, persistentInit + (container.restartPolicy === 'Always' ? 0 : count));
        }
        const extended = Math.max(regularDemand + persistentInit, initPeak);
        const claimDemand = (pod.resourceClaims || []).reduce((sum: number, ref: JsonRecord) => {
            const claim = claims.find(candidate => candidate.metadata?.name === (ref.resourceClaimTemplateName || ref.resourceClaimName)
                && (candidate.metadata?.namespace || 'default') === (document.metadata?.namespace || 'default'));
            const requests = (claim?.kind === 'ResourceClaimTemplate' ? claim.spec?.spec : claim?.spec)?.devices?.requests || [];
            return sum + requests.reduce((count: number, request: JsonRecord) => {
                const alternatives = request.exactly ? [request.exactly] : request.firstAvailable || [request];
                return count + Math.max(0, ...alternatives.filter((item: JsonRecord) => /gpu|gaudi|xpu|tpu/i.test(item.deviceClassName || '')).map((item: JsonRecord) => Number(item.count ?? 1)));
            }, 0);
        }, 0);
        const modelTp = containers.filter((container: JsonRecord) => container.name === 'modelserver').reduce((sum: number, container: JsonRecord) => sum + Number(runtimeArgument(container, 'tensor-parallel-size') || 1), 0);
        return total + Number(document.spec?.replicas ?? 1) * Math.max(extended + claimDemand, modelTp);
    }, 0);
    // Replace an earlier capacity error when custom patches change the final demand.
    for (let i = errors.length - 1; i >= 0; i--) if (errors[i].startsWith('Insufficient resources:')) errors.splice(i, 1);
    if (!Number.isFinite(demand) || demand > budget || (budget === 0 && demand === 0)) {
        errors.push(`Insufficient resources: this configuration requires ${demand} accelerators; the ${basis === 'total' ? 'physical' : 'currently available'} budget is ${budget}. Reduce replicas / TP or choose a sufficient resource budget before generating YAML.`);
    }
    return demand;
}

// A DRA device class is an accelerator when the selected cluster advertises it
// (multi-vendor, discovered device classes) or it matches the known accelerator
// naming conventions. The cluster list makes new hardware work without a regex
// change; the regex is the fallback when no cluster snapshot is available.
export function isAcceleratorDeviceClass(deviceClass: string, clusterDeviceClasses: unknown): boolean {
    const value = String(deviceClass || '');
    if (!value) return false;
    const known = Array.isArray(clusterDeviceClasses) ? clusterDeviceClasses.map(String) : [];
    if (known.includes(value)) return true;
    return /^(?:gpu\.|.*\.gpu\.|nvidia\.com|amd\.com)/.test(value);
}

// The Guide's upstream variant (xpu/gpu) is a vendor identity, so it must follow
// the cluster actually being deployed to. Detection covers both access modes: a
// DRA driver/device class or an extended resource in node allocatable
// (e.g. nvidia.com/gpu), so an extended-resource cluster is still resolved.
export function resolveAcceleratorVariant(requested: string, cluster: JsonRecord | null): string {
    if (!cluster) return requested;
    const resourceKeys = Object.keys(cluster.allocatable?.accelerators || {}).map((key) => key.toLowerCase());
    const deviceClasses = (cluster.deviceClasses || []).map((value: unknown) => String(value).toLowerCase());
    const driver = String(cluster.accelerator?.devices?.[0]?.driver || '').toLowerCase();
    const has = (needle: string) => resourceKeys.some((key) => key.includes(needle)) || deviceClasses.some((value) => value.includes(needle)) || driver.includes(needle);
    const intel = has('intel');
    const nvidia = has('nvidia');
    if (nvidia && !intel) return 'gpu';
    if (intel && !nvidia) return 'xpu';
    return requested;
}

export function planDocuments(documents: any[], model: string, machine: JsonRecord, cluster: JsonRecord | null, requested: JsonRecord = {}) {
    const patches: JsonRecord[] = [];
    const warnings: string[] = [];
    const errors: string[] = [];
    let guideSettings: { cacheCpuGiB?: number; rdmaNicCount?: number; routerValues?: string } = {};
    try { guideSettings = normalizeGuideSettings(requested.guide, requested.guideVariant, requested.guideSettings); }
    catch (error) { errors.push(error instanceof Error ? error.message : String(error)); }
    let nicRequests = 0;
    const modelEstimate = estimateModel(model);
    const clusterAccelerators = cluster
        ? Number(cluster.accelerator?.count || 0)
            || Object.values(cluster.allocatable?.accelerators || {}).reduce((sum: number, value) => sum + Number(value), 0)
        : 0;
    const clusterDevices: JsonRecord[] = cluster?.accelerator?.devices || [];
    const normalizePciAddress = (value: unknown) => String(value || '').trim().toLowerCase();
    const clusterPciAddresses = new Set(clusterDevices.map((device) => normalizePciAddress(device.pciAddress)).filter(Boolean));
    const configuredDevices = String(process.env.PRISM_GPU_PCI_ALLOWLIST || '').split(',').map((item) => item.trim()).filter(Boolean);
    const targetNode = String(process.env.PRISM_K8S_TARGET_NODE || '').trim();
    const clusterNodeNames = new Set((cluster?.nodes || []).map((node: JsonRecord) => String(node.name || '')).filter(Boolean));
    if (cluster && targetNode && clusterNodeNames.size && !clusterNodeNames.has(targetNode)) {
        errors.push(`Selected cluster does not contain configured target node ${targetNode}; choose the cluster that owns the configured GPU allowlist.`);
    }
    // ResourceSlice discovery is a separate Kubernetes API call from node discovery and may
    // transiently fail or return no device attributes while a cluster session is otherwise
    // healthy. An empty PCI inventory is not evidence that every configured card is absent.
    // Trust the launcher's node-scoped allowlist in that case; when inventory is available,
    // continue to reject any real mismatch.
    const hasPciInventory = clusterPciAddresses.size > 0;
    const invalidConfiguredDevices = hasPciInventory
        ? configuredDevices.filter((address) => !clusterPciAddresses.has(normalizePciAddress(address)))
        : [];
    if (cluster && hasPciInventory && invalidConfiguredDevices.length) {
        errors.push(`Configured GPU PCI address(es) are not present in the selected cluster: ${invalidConfiguredDevices.join(', ')}`);
    }
    if (cluster && configuredDevices.length && !hasPciInventory) {
        warnings.push(`Kubernetes did not return DRA PCI-address inventory; using the ${configuredDevices.length}-device allowlist pinned to ${targetNode || 'the Lens target node'}.`);
    }
    const validConfiguredDevices = hasPciInventory
        ? configuredDevices.filter((address) => clusterPciAddresses.has(normalizePciAddress(address)))
        : configuredDevices;
    const scopedAccelerators = validConfiguredDevices.length || clusterAccelerators || Number(machine.accelerator?.count || 0);
    const totalAccelerators = clusterAccelerators || Number(machine.accelerator?.count || 0);
    const resourceBasis = requested.resourceBasis === 'total' ? 'total' : 'available';
    const observedBudget = Number(requested.recommendationGpuCount);
    const availableAccelerators = resourceBasis === 'total' ? totalAccelerators
        : Number.isSafeInteger(observedBudget) && observedBudget >= 0 ? Math.min(scopedAccelerators, observedBudget) : scopedAccelerators;
    if (resourceBasis === 'total' && validConfiguredDevices.length && totalAccelerators > validConfiguredDevices.length) warnings.push(`Recommendations include all ${totalAccelerators} physical cards; scheduling remains restricted to the ${validConfiguredDevices.length}-card allowlist until its scope is expanded.`);
    const claimCountByRole: Record<string, number> = {};
    let guideReplicas = 0;
    let plannedReplicas = 0;
    let tensorParallelSize = 1;
    const tensorParallelSizeByRole: Record<string, number> = {};
    const guideReplicasByRole: Record<string, number> = {};
    const plannedReplicasByRole: Record<string, number> = {};
    let acceleratorsPerReplica = 0;
    let cpuPerReplica = 0;
    let memoryPerReplicaGiB = 0;
    const modelSource = String(requested.modelSource || (requested.modelPath ? 'shared-path' : 'huggingface'));
    const modelHostPath = String(requested.modelPath || '').trim();
    if (modelHostPath && (!path.posix.isAbsolute(modelHostPath) || modelHostPath.split('/').includes('..') || /[\r\n]/.test(modelHostPath))) {
        errors.push('Shared model path must be an absolute worker-node path without .. segments or newlines.');
    }
    // A Model Cache storage volume of kind nfs/dynamic-pvc has no hostPath:
    // it is backed by an already-provisioned PVC (see storage_mount.py on the
    // Python side, which every actual Deploy provider resolves the same way).
    // This preview renderer has no Kubernetes client of its own, so the PVC
    // claim name is supplied directly by the caller (already fetched from the
    // Storage API) instead of being looked up here.
    const modelPvcClaimName = String(requested.modelPvcClaimName || '').trim();
    const modelUsesPvc = modelSource === 'auto-cache' && Boolean(modelPvcClaimName);
    const modelArgument = modelSource === 'shared-path' && modelHostPath ? '/model-cache' : model;
    // Only genuinely dual-role guides (prefill AND decode as independent workloads) send
    // role-specific overrides (prefillReplicas/decodeTensorParallelSize/...). Single-role guides
    // that happen to label their sole Deployment/ResourceClaimTemplate 'decode' or 'prefill' for
    // other reasons (e.g. tiered-prefix-cache) must still honor the flat replicas/tensorParallelSize
    // the user actually requested instead of silently falling back to the guide's hardcoded defaults.
    const isDualRoleGuide = requested.guide === 'pd-disaggregation';

    const replace = (container: JsonRecord, key: string | number, value: any, pointer: string, reason: string) => {
        if (container[key] === value) return;
        patches.push({ op: 'replace', path: pointer, from: container[key], value, reason });
        container[key] = value;
    };
    const resourceRole = (value: JsonRecord, fallback = 'serving') => {
        const labelRole = String(value.metadata?.labels?.['llm-d.ai/role'] || '').toLowerCase();
        const resourceName = String(value.metadata?.name || '').toLowerCase();
        if (labelRole === 'prefill' || resourceName.includes('prefill')) return 'prefill';
        if (labelRole === 'decode' || resourceName.includes('decode')) return 'decode';
        return fallback;
    };
    documents.forEach((document, documentIndex) => {
        if (document?.kind !== 'ResourceClaimTemplate') return;
        const role = resourceRole(document);
        const requestedTp = Number(requested[`${role}TensorParallelSize`] || ((role === 'serving' || !isDualRoleGuide) ? requested.tensorParallelSize : 0));
        const requests = document.spec?.spec?.devices?.requests || [];
        if (!requests.length) errors.push(`ResourceClaimTemplate ${document.metadata?.name || documentIndex} has no DRA device requests.`);
        for (let requestIndex = 0; requestIndex < requests.length; requestIndex += 1) {
            const exactly = requests[requestIndex]?.exactly;
            if (!exactly) {
                errors.push(`ResourceClaimTemplate ${document.metadata?.name || documentIndex} uses an unsupported DRA request shape.`);
                continue;
            }
            const deviceClassName = String(exactly.deviceClassName || '');
            if (cluster && deviceClassName && !cluster.deviceClasses?.includes(deviceClassName)) {
                errors.push(`DRA device class ${deviceClassName} is not available in the selected cluster.`);
            }
            if (deviceClassName === 'dranet-rdma') {
                nicRequests++;
                if (guideSettings.rdmaNicCount != null) replace(exactly, 'count', guideSettings.rdmaNicCount, `/documents/${documentIndex}/spec/spec/devices/requests/${requestIndex}/exactly/count`, 'apply requested NIC count independently of GPU topology');
            }
            if (!isAcceleratorDeviceClass(deviceClassName, cluster?.deviceClasses)) continue;
            if (requestedTp > 0 && Number(exactly.count) !== requestedTp) {
                replace(exactly, 'count', requestedTp, `/documents/${documentIndex}/spec/spec/devices/requests/${requestIndex}/exactly/count`, 'match accelerator claim to requested tensor parallel size');
            }
            const claimCount = requestedTp > 0 ? requestedTp : Number(exactly.count || 0);
            claimCountByRole[role] = Math.max(claimCountByRole[role] || 0, claimCount);
            if (validConfiguredDevices.length) {
                const attributeDomain = String(cluster?.accelerator?.devices?.[0]?.driver || 'gpu.intel.com');
                const selectors = [...(exactly.selectors || []), { cel: { expression: `device.attributes[${JSON.stringify(attributeDomain)}].pciAddress in [${validConfiguredDevices.map((item) => JSON.stringify(item)).join(', ')}]` } }];
                replace(exactly, 'selectors', selectors, `/documents/${documentIndex}/spec/spec/devices/requests/${requestIndex}/exactly/selectors`, 'restrict allocation to configured devices present in the selected cluster');
            }
        }
    });
    const walk = (value: any, pointer = '', inheritedRole = 'serving') => {
        if (Array.isArray(value)) {
            value.forEach((item, index) => { if (typeof item !== 'string') walk(item, `${pointer}/${index}`, inheritedRole); });
            return;
        }
        if (!value || typeof value !== 'object') return;
        const currentRole = resourceRole(value, inheritedRole);
        if (value.kind === 'Deployment' && value.spec) {
            if (Number(value.spec.progressDeadlineSeconds || 0) < 1800) {
                replace(value.spec, 'progressDeadlineSeconds', 1800, `${pointer}/spec/progressDeadlineSeconds`, 'allow XPU DRA allocation and model initialization to complete');
            }
            const role = currentRole;
            const guideCount = Number(value.spec.replicas || 1);
            guideReplicas += guideCount;
            guideReplicasByRole[role] = (guideReplicasByRole[role] || 0) + guideCount;
            const containers = value.spec.template?.spec?.containers || [];
            let perReplica = 0;
            let deploymentCpu = 0;
            let deploymentMemory = 0;
            for (const container of containers) {
                if (container.name === 'modelserver' && requested.runtimeImage) {
                    const image = pinModelServerImage(String(requested.runtimeImage));
                    replace(container, 'image', image, `${pointer}/spec/template/spec/containers/${containers.indexOf(container)}/image`, 'apply selected runtime image');
                }
                if (container.name === 'modelserver' && Array.isArray(container.env)) {
                    for (const item of container.env) {
                        if (item?.valueFrom?.secretKeyRef?.name === 'llm-d-hf-token') item.valueFrom.secretKeyRef.optional = true;
                    }
                }
                if (container.name === 'modelserver' && (modelHostPath || modelUsesPvc)) {
                    const podSpec = value.spec.template?.spec || {};
                    const volumes = Array.isArray(podSpec.volumes) ? podSpec.volumes : [];
                    if (!Array.isArray(podSpec.volumes)) podSpec.volumes = volumes;
                    const existingVolume = volumes.find((item: JsonRecord) => item?.name === 'model-cache');
                    const volumeSource = modelUsesPvc
                        ? { persistentVolumeClaim: { claimName: modelPvcClaimName } }
                        : { hostPath: { path: modelHostPath, type: 'DirectoryOrCreate' } };
                    if (existingVolume) {
                        delete existingVolume.hostPath;
                        delete existingVolume.persistentVolumeClaim;
                        Object.assign(existingVolume, volumeSource);
                    } else volumes.push({ name: 'model-cache', ...volumeSource });
                    const mounts = Array.isArray(container.volumeMounts) ? container.volumeMounts : [];
                    if (!Array.isArray(container.volumeMounts)) container.volumeMounts = mounts;
                    if (!mounts.some((item: JsonRecord) => item?.name === 'model-cache')) mounts.push({ name: 'model-cache', mountPath: '/model-cache', readOnly: modelSource === 'shared-path' });
                    if (modelSource === 'auto-cache') {
                        const environment = Array.isArray(container.env) ? container.env : [];
                        if (!Array.isArray(container.env)) container.env = environment;
                        setEnvironmentValue(environment, 'HF_HOME', '/model-cache');
                        setEnvironmentValue(environment, 'HF_HUB_DISABLE_XET', '1');
                        // The model is already in the mounted cache; keep Hugging Face
                        // offline so vLLM startup does not depend on outbound Hub access.
                        setEnvironmentValue(environment, 'HF_HUB_OFFLINE', '1');
                        setEnvironmentValue(environment, 'TRANSFORMERS_OFFLINE', '1');
                    } else {
                        // shared-path serves the mounted directory locally; keep Hugging Face
                        // offline so startup never depends on outbound Hub access.
                        const environment = (Array.isArray(container.env) ? container.env : [])
                            .filter((item: JsonRecord) => item?.valueFrom?.secretKeyRef?.name !== 'llm-d-hf-token');
                        container.env = environment;
                        setEnvironmentValue(environment, 'HF_HUB_OFFLINE', '1');
                        setEnvironmentValue(environment, 'TRANSFORMERS_OFFLINE', '1');
                    }
                }
                const proxyVariables = [
                    ['HTTP_PROXY', process.env.HTTP_PROXY || process.env.http_proxy],
                    ['HTTPS_PROXY', process.env.HTTPS_PROXY || process.env.https_proxy],
                    ['NO_PROXY', process.env.NO_PROXY || process.env.no_proxy],
                ].filter((entry) => entry[1]);
                if (proxyVariables.length) {
                    const env = Array.isArray(container.env) ? container.env : [];
                    if (!Array.isArray(container.env)) replace(container, 'env', env, `${pointer}/spec/template/spec/containers/${containers.indexOf(container)}/env`, 'add runtime environment');
                    for (const [name, proxyValue] of proxyVariables) {
                        const existing = env.find((item: JsonRecord) => item?.name === name);
                        if (existing) replace(existing, 'value', proxyValue, `${pointer}/spec/template/spec/containers/${containers.indexOf(container)}/env/${env.indexOf(existing)}/value`, 'apply deployment network proxy');
                        else {
                            env.push({ name, value: proxyValue });
                            patches.push({ op: 'add', path: `${pointer}/spec/template/spec/containers/${containers.indexOf(container)}/env/-`, value: { name, value: proxyValue }, reason: 'apply deployment network proxy' });
                        }
                    }
                }
                const limits = container.resources?.limits || container.resources?.requests || {};
                const accelerator = acceleratorResource(limits);
                if (accelerator) perReplica += accelerator[1];
                deploymentCpu += quantityNumber(limits.cpu);
                deploymentMemory += quantityNumber(limits.memory);
            }
            const initContainers = value.spec.template?.spec?.initContainers || [];
            for (const container of initContainers) {
                if (container.name === 'routing-proxy' && requested.guide === 'pd-disaggregation') {
                    replace(container, 'image', PD_ROUTING_PROXY_IMAGE, `${pointer}/spec/template/spec/initContainers/${initContainers.indexOf(container)}/image`, 'pin routing proxy to the Guide release');
                    if (container.imagePullPolicy === 'Always') {
                        replace(container, 'imagePullPolicy', 'IfNotPresent', `${pointer}/spec/template/spec/initContainers/${initContainers.indexOf(container)}/imagePullPolicy`, 'avoid mutable nightly proxy pulls');
                    }
                    if (Array.isArray(container.args)) setArgument(container.args, 'vllm-port', 8200);
                }
            }
            perReplica = Math.max(perReplica, claimCountByRole[role] || claimCountByRole.serving || 0);
            acceleratorsPerReplica = Math.max(acceleratorsPerReplica, perReplica);
            cpuPerReplica = Math.max(cpuPerReplica, deploymentCpu);
            memoryPerReplicaGiB = Math.max(memoryPerReplicaGiB, deploymentMemory);
            const requestedReplicas = Number(requested[`${role}Replicas`] || ((role === 'serving' || !isDualRoleGuide) ? requested.replicas : 0));
            let capacity = Number.POSITIVE_INFINITY;
            const availableCpu = cluster?.allocatable?.cpu || machine.cpu?.logicalCores;
            const availableMemory = cluster?.allocatable?.memoryGiB || machine.memoryGiB;
            if (deploymentCpu > 0) capacity = Math.min(capacity, Math.floor(availableCpu / deploymentCpu));
            if (deploymentMemory > 0) capacity = Math.min(capacity, Math.floor(availableMemory / deploymentMemory));
            capacity = Math.max(0, capacity);
            if (requestedReplicas > capacity) errors.push(`${role} replicas=${requestedReplicas} exceed selected cluster CPU/RAM capacity (${capacity}); reduce the topology in the configuration editor.`);
            // Preserve explicit topology for offline planning and resource-constrained clusters.
            // Validate the preserved topology against the selected GPU budget below.
            if (availableAccelerators > 0 && perReplica > 0) capacity = Math.min(capacity, Math.floor(availableAccelerators / perReplica));
            if (!Number.isFinite(capacity)) capacity = guideCount;
            const plannedCount = requestedReplicas > 0 ? requestedReplicas : Math.min(guideCount, capacity);
            if (plannedCount !== guideCount) replace(value.spec, 'replicas', plannedCount, `${pointer}/spec/replicas`, requestedReplicas > 0 ? 'apply requested topology' : 'fit detected machine or cluster capacity');
            plannedReplicas += plannedCount;
            plannedReplicasByRole[role] = (plannedReplicasByRole[role] || 0) + plannedCount;
        }
        for (const [key, child] of Object.entries(value)) {
            if (key === 'model' && typeof child === 'string') replace(value, key, modelArgument, `${pointer}/${key}`, modelHostPath ? 'serve selected shared model directory' : 'replace guide model');
            else if (key === 'llm-d.ai/model' && typeof child === 'string') replace(value, key, model.split('/').pop(), `${pointer}/${key}`, 'update model label');
            else {
                walk(child, `${pointer}/${key.replace(/~/g, '~0').replace(/\//g, '~1')}`, currentRole);
            }
        }
    };
    documents.forEach((document, index) => walk(document, `/documents/${index}`));
    try {
        const overrides = runtimeOverrides(requested);
        const applied = new Set<object>();
        for (const document of documents) {
            if (document.kind !== 'Deployment') continue;
            const role = resourceRole(document);
            for (const container of document.spec?.template?.spec?.containers || []) {
                if (container.name !== 'modelserver') continue;
                const selected = overrides.filter(item => item.target === 'both' || item.target === (isDualRoleGuide ? role : 'decode'));
                const before = structuredClone(container);
                const requestedTp = Number(requested[`${role}TensorParallelSize`] || ((!isDualRoleGuide || role === 'serving') ? requested.tensorParallelSize : 0));
                configureModelServer(container, modelArgument, requestedTp, model, requested.guide === 'precise-prefix-cache-routing');
                if (requestedTp > 0) { tensorParallelSize = requestedTp; tensorParallelSizeByRole[role] = requestedTp; }
                applyRuntimeOverrides(container, selected);
                if (requested.guide === 'precise-prefix-cache-routing') {
                    const events = JSON.parse(runtimeArgument(container, 'kv-events-config') || '{}');
                    if (typeof events.topic !== 'string' || !/^kv@[^@]+@.+$/.test(events.topic) || events.topic.split('@').slice(2).join('@') !== model) throw new Error('Precise KV event topic must match the configured model.');
                }
                if (guideSettings.cacheCpuGiB != null) {
                    if (selected.some(item => item.name === 'kv-transfer-config' || item.name === 'LMCACHE_MAX_LOCAL_CPU_SIZE')) throw new Error('CPU cache capacity conflicts with a custom connector/cache override.');
                    configureCpuCache(container, guideSettings.cacheCpuGiB, requested.guideVariant);
                }
                for (const key of ['command', 'args', 'env']) {
                    if (JSON.stringify(before[key]) !== JSON.stringify(container[key])) patches.push({
                        op: before[key] == null ? 'add' : 'replace',
                        path: `/documents/${documents.indexOf(document)}/spec/template/spec/containers/${document.spec.template.spec.containers.indexOf(container)}/${key}`,
                        from: before[key], value: container[key], reason: 'apply explicit runtime configuration',
                    });
                }
                selected.forEach(item => applied.add(item));
            }
        }
        for (const item of overrides) if (!applied.has(item)) errors.push(`No model server matched runtime override ${item.target}:${item.name}.`);
    } catch (error) {
        errors.push(error instanceof Error ? error.message : String(error));
    }
    if (guideSettings.rdmaNicCount != null && !nicRequests) errors.push('The selected Guide has no supported RDMA NIC request.');
    if (!documents.some(document => document.kind === 'Deployment' && document.spec?.template?.spec?.containers?.some(container => container.name === 'modelserver'))) errors.push('The selected Guide has no identifiable modelserver Deployment.');
    if (!guideReplicas) warnings.push('No Deployment replica field was found; replica capacity was inherited from the guide unchanged.');

    for (const [role, claimCount] of Object.entries(claimCountByRole)) {
        const roleTp = tensorParallelSizeByRole[role];
        if (roleTp && claimCount && roleTp !== claimCount) warnings.push(`${role} TP=${roleTp}, while the manifest requests ${claimCount} accelerator(s) per replica.`);
    }
    // Require a usable DRA claim when the Guide actually uses DRA; an
    // extended-resource Guide carries its TP in container resources instead.
    const guideUsesDraClaims = documents.some(document => document.kind === 'ResourceClaimTemplate');
    if (tensorParallelSize > 1 && guideUsesDraClaims && !Object.keys(claimCountByRole).length) errors.push(`Requested TP=${tensorParallelSize}, but the selected Guide has no usable DRA ResourceClaimTemplate.`);
    const reportedAcceleratorMemory = Number(cluster?.accelerator?.memoryGiB || machine.accelerator?.memoryGiB || 0);
    const acceleratorMemory = reportedAcceleratorMemory || knownAcceleratorMemoryGiB(
        cluster?.accelerator?.model || machine.accelerator?.model
    );
    const acceleratorMemorySource = reportedAcceleratorMemory ? 'cluster-reported' : acceleratorMemory ? 'known-model-fallback' : null;
    if (acceleratorMemorySource === 'known-model-fallback') {
        warnings.push(`Accelerator memory was not reported by DRA; using the known ${acceleratorMemory} GiB capacity for ${cluster?.accelerator?.model || machine.accelerator?.model}.`);
    }
    const recommendedTensorParallelSize = modelEstimate.estimatedWeightMemoryGiB && acceleratorMemory
        ? Math.max(1, Math.ceil(modelEstimate.estimatedWeightMemoryGiB / (acceleratorMemory * 0.9)))
        : null;
    const hasDisaggregatedRequest = requested.guide === 'pd-disaggregation';
    if (recommendedTensorParallelSize) {
        const requestedRoles = hasDisaggregatedRequest
            ? [['prefill', Number(requested.prefillTensorParallelSize || tensorParallelSizeByRole.prefill || 1)], ['decode', Number(requested.decodeTensorParallelSize || tensorParallelSizeByRole.decode || 1)]] as const
            : [['serving', Number(requested.tensorParallelSize || tensorParallelSize)]] as const;
        for (const [role, roleTp] of requestedRoles) {
            const label = hasDisaggregatedRequest ? `${role[0].toUpperCase()}${role.slice(1)} TP` : 'TP';
            const availableMemory = acceleratorMemory * roleTp;
            if (roleTp < recommendedTensorParallelSize) errors.push(`${label}=${roleTp} is too small for the estimated ${modelEstimate.estimatedWeightMemoryGiB} GiB model footprint on ${acceleratorMemory} GiB accelerators. Recommended TP is at least ${recommendedTensorParallelSize}.`);
            else if (modelEstimate.estimatedWeightMemoryGiB > availableMemory) errors.push(`Estimated model weight memory ${modelEstimate.estimatedWeightMemoryGiB} GiB exceeds ${label}=${roleTp} accelerator memory ${availableMemory} GiB.`);
        }
    } else warnings.push(modelEstimate.assumption);
    // Run accelerator workloads under the vendor's RuntimeClass when the cluster
    // defines one; the upstream guides omit it and rely on it being the node
    // default, which is not true here (runc default + an nvidia RuntimeClass).
    const runtimeClass = runtimeClassForAccelerator(cluster, String(requested.accelerator || ''));
    if (runtimeClass) {
        for (const document of documents) {
            const podSpec = workloadPodSpec(document);
            if (!podSpec || podSpec.runtimeClassName) continue;
            podSpec.runtimeClassName = runtimeClass;
            patches.push({
                op: 'add',
                path: `/documents/${documents.indexOf(document)}/spec/template/spec/runtimeClassName`,
                value: runtimeClass,
                reason: `run under the ${runtimeClass} container runtime so the container can access the accelerator`,
            });
        }
    }
    const requestedAccelerators = validateManifestCapacity(documents, availableAccelerators, resourceBasis, errors);
    if (cluster && !cluster.preflightPassed) errors.push('Kubernetes preflight did not pass.');
    return {
        documents,
        planningPatch: patches,
        validation: { status: errors.length ? 'invalid' : warnings.length ? 'warnings' : 'valid', errors, warnings },
        summary: {
            guideReplicas,
            plannedReplicas: plannedReplicas || guideReplicas || null,
            tensorParallelSize,
            tensorParallelSizeByRole,
            guideReplicasByRole,
            plannedReplicasByRole,
            acceleratorsPerReplica,
            cpuPerReplica,
            memoryPerReplicaGiB,
            availableAccelerators,
            resourceBasis,
            totalAccelerators,
            acceleratorSource: validConfiguredDevices.length ? 'validated-pci-allowlist' : cluster?.accelerator?.source || 'machine-discovery',
            selectedDevicePciAddresses: validConfiguredDevices,
            modelEstimate,
            modelSource: modelHostPath
                ? { mode: modelSource, path: modelHostPath, id: model }
                : modelUsesPvc
                    ? { mode: modelSource, pvcClaimName: modelPvcClaimName, id: model }
                    : { mode: 'huggingface', id: model },
            acceleratorMemoryGiB: acceleratorMemory || null,
            acceleratorMemorySource,
            recommendedTensorParallelSize,
            maxRecommendedReplicas: (recommendedTensorParallelSize || tensorParallelSize)
                ? Math.floor(availableAccelerators / (recommendedTensorParallelSize || tensorParallelSize))
                : null,
            requestedAccelerators,
        },
    };
}

function deploymentContract(documents: any[], guide: string): JsonRecord {
    const deployments = documents.filter((item) => item?.kind === 'Deployment' && item.metadata?.name);
    if (!deployments.length) throw Object.assign(new Error('Generated Guide YAML contains no Deployment'), { status: 422 });
    const readinessDeployments = deployments.map((item) => String(item.metadata.name));
    let service = documents.find((item) => item?.kind === 'Service' && item.metadata?.name && item.spec?.ports?.length);
    if (!service) {
        const serving = deployments.find((item) => ['decode', 'serving'].includes(String(item.metadata?.labels?.['llm-d.ai/role'] || ''))) || deployments[0];
        const containerPort = serving.spec?.template?.spec?.containers?.flatMap((container: JsonRecord) => container.ports || [])
            .find((port: JsonRecord) => Number(port.containerPort) > 0);
        const port = Number(containerPort?.containerPort || 8000);
        service = {
            apiVersion: 'v1',
            kind: 'Service',
            metadata: { name: `${guide}-modelserver`, labels: { 'llm-d.ai/guide': guide } },
            spec: {
                selector: serving.spec?.selector?.matchLabels || serving.spec?.template?.metadata?.labels || {},
                ports: [{ name: 'http', port, targetPort: containerPort?.name || port }],
            },
        };
        documents.push(service);
    }
    const endpointPort = Number(service.spec.ports[0].port);
    return {
        readinessDeployments,
        endpoint: { protocol: 'http', serviceName: String(service.metadata.name), port: endpointPort },
    };
}

export function selectManifestPaths(paths: string[], guide: string, accelerator: string, requestedModelServer: string, requestedVariant = '') {
    let modelServer = requestedModelServer;
    let prefix = `guides/${guide}/modelserver/${accelerator}/${modelServer}/`;
    let matching = paths.filter((item) => item.startsWith(prefix));
    if (!matching.length) throw Object.assign(new Error('No official YAML manifests match the selected guide, accelerator, and model server'), { status: 404 });
    let normalizedVariant = requestedVariant.trim().replace(/^\/+|\/+$/g, '');
    if (normalizedVariant && (normalizedVariant.split('/').includes('..') || !/^[A-Za-z0-9._/-]+$/.test(normalizedVariant))) {
        throw Object.assign(new Error('Guide variant path is invalid'), { status: 400 });
    }
    const explicitVariantPrefix = `${prefix}${normalizedVariant}/`;
    if (normalizedVariant && !matching.some((item) => item.startsWith(explicitVariantPrefix))) {
        const alternatePrefix = `guides/${guide}/modelserver/${accelerator}/${normalizedVariant}/`;
        const alternateMatching = paths.filter((item) => item.startsWith(alternatePrefix));
        const alternateRoots = alternateMatching.filter((item) => !item.slice(alternatePrefix.length).includes('/'));
        if (alternateRoots.length) {
            modelServer = normalizedVariant;
            prefix = alternatePrefix;
            matching = alternateMatching;
            normalizedVariant = '';
        }
    }
    const relative = matching.map((item) => item.slice(prefix.length));
    const roots = relative.filter((item) => !item.includes('/'));
    const variant = normalizedVariant || (roots.length ? '.' : relative.some((item) => item.startsWith('base/')) ? 'base' : relative[0].split('/')[0]);
    const selected = matching.filter((item) => variant === '.' ? !item.slice(prefix.length).includes('/') : item.startsWith(`${prefix}${variant}/`));
    if (!selected.length) throw Object.assign(new Error(`No official YAML manifests match Guide variant ${variant}`), { status: 404 });
    const guidePath = variant === '.' ? prefix.slice(0, -1) : `${prefix}${variant}`;
    return { modelServer, variant, selected, guidePath };
}

export async function loadManifests(guide: string, accelerator: string, modelServer: string, requestedVariant = '', clusterSelection: { clusterId?: unknown; clusterSessionId?: unknown } = {}) {
    const cache = await getCatalog(clusterSelection);
    const selection = selectManifestPaths(cache.paths, guide, accelerator, modelServer, requestedVariant);
    modelServer = selection.modelServer;
    const { variant, selected, guidePath } = selection;
    return loadCachedManifest(null, async () => {
        const kustomizeSource = path.join(cache.localRoot, guidePath);
        const [files, { stdout: rendered }] = await Promise.all([
            Promise.resolve(selected.map((manifestPath) => ({
                path: manifestPath,
                content: fs.readFileSync(path.join(cache.localRoot, manifestPath), 'utf8'),
            }))),
            execFileAsync('kubectl', ['kustomize', kustomizeSource], { encoding: 'utf8', timeout: 60_000, maxBuffer: 16 * 1024 * 1024 }),
        ]);
        if (!rendered) {
            throw Object.assign(new Error('Could not render the selected official guide with kubectl kustomize'), { status: 502 });
        }
        return {
            modelServer,
            localRoot: cache.localRoot,
            variant,
            files,
            rendered,
            source: { repository: GITHUB_REPOSITORY, requestedRef: cache.ref, commit: cache.commit },
        };
    });
}

function normalizedRepository(value: string): string {
    const trimmed = value.trim().replace(/\/$/, '').replace(/\.git$/, '');
    const match = trimmed.match(/^(?:https:\/\/github\.com\/)?([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)$/i);
    if (!match) throw Object.assign(new Error('Remote repository must be a GitHub owner/repository name or https://github.com/owner/repository URL'), { status: 400 });
    return match[1];
}

function normalizedRepositoryPath(value: string): string {
    const normalized = value.trim().replace(/^\/+/, '').replace(/\\/g, '/');
    if (!normalized || normalized.split('/').includes('..')) {
        throw Object.assign(new Error('Repository YAML path must be a relative path without .. segments'), { status: 400 });
    }
    return normalized;
}

async function loadRemoteManifest(source: JsonRecord): Promise<ManifestSet> {
    const repository = normalizedRepository(String(source.repository || ''));
    const requestedRef = String(source.ref || source.branch || 'main').trim();
    const manifestPath = normalizedRepositoryPath(String(source.path || ''));
    const hasInvalidRefCharacter = Array.from(requestedRef)
        .some((character) => /\s/.test(character) || character.charCodeAt(0) <= 0x1f);
    if (!requestedRef || hasInvalidRefCharacter) {
        throw Object.assign(new Error('Remote branch/ref is invalid'), { status: 400 });
    }
    const refPayload = JSON.parse(await fetchText(`${GITHUB_API}/repos/${repository}/commits/${encodeURIComponent(requestedRef)}`)) as JsonRecord;
    const commit = String(refPayload.sha || '');
    if (!/^[0-9a-f]{40}$/.test(commit)) throw Object.assign(new Error('Remote branch/ref could not be resolved to a commit'), { status: 404 });
    const content = await fetchText(
        githubContentUrl(repository, manifestPath, commit),
        'application/vnd.github.raw+json',
    );
    const fileName = path.posix.basename(manifestPath).toLowerCase();
    const kustomizePath = ['kustomization.yaml', 'kustomization.yml'].includes(fileName)
        ? path.posix.dirname(manifestPath)
        : null;
    const rendered = kustomizePath
        ? commandOutput('kubectl', ['kustomize', `https://github.com/${repository}//${kustomizePath}?ref=${commit}`], 60_000)
        : content;
    return {
        variant: 'custom-remote',
        files: [{ path: manifestPath, content }],
        rendered,
        source: { repository, requestedRef, commit },
    };
}

function allowedLocalRoots(): string[] {
    const configured = process.env.PRISM_GUIDE_LOCAL_ROOTS
        || process.cwd();
    return configured.split(path.delimiter).filter(Boolean).map((item) => path.resolve(item));
}

function loadLocalManifest(source: JsonRecord): ManifestSet {
    const requestedPath = path.resolve(String(source.path || '').trim());
    const allowed = allowedLocalRoots();
    if (!allowed.some((root) => requestedPath === root || requestedPath.startsWith(`${root}${path.sep}`))) {
        throw Object.assign(new Error(`Local YAML path must be under an allowed root: ${allowed.join(', ')}`), { status: 403 });
    }
    if (!fs.existsSync(requestedPath)) throw Object.assign(new Error('Local YAML/Kustomize path does not exist'), { status: 404 });
    const stat = fs.statSync(requestedPath);
    const kustomization = stat.isDirectory()
        ? ['kustomization.yaml', 'kustomization.yml'].map((name) => path.join(requestedPath, name)).find((item) => fs.existsSync(item) && fs.statSync(item).isFile())
        : ['kustomization.yaml', 'kustomization.yml'].includes(path.basename(requestedPath).toLowerCase()) ? requestedPath : null;
    const inputFile = kustomization || requestedPath;
    if (!fs.statSync(inputFile).isFile() || !/\.ya?ml$/i.test(inputFile)) {
        throw Object.assign(new Error('Local source must be a YAML file or a directory containing kustomization.yaml'), { status: 400 });
    }
    const content = fs.readFileSync(inputFile, 'utf8');
    const rendered = kustomization ? commandOutput('kubectl', ['kustomize', path.dirname(kustomization)], 60_000) : content;
    const gitRoot = commandOutput('git', ['-C', path.dirname(inputFile), 'rev-parse', '--show-toplevel']);
    const gitCommit = commandOutput('git', ['-C', path.dirname(inputFile), 'rev-parse', 'HEAD']);
    const commit = /^[0-9a-f]{40}$/.test(gitCommit) ? gitCommit : crypto.createHash('sha1').update(rendered).digest('hex');
    return {
        variant: 'custom-local',
        localRoot: gitRoot || path.dirname(inputFile),
        files: [{ path: inputFile, content }],
        rendered,
        source: { repository: gitRoot || path.dirname(inputFile), requestedRef: String(source.ref || 'working-tree'), commit },
    };
}

async function loadSelectedManifests(guide: string, accelerator: string, modelServer: string, source: JsonRecord, variant = '', clusterSelection: { clusterId?: unknown; clusterSessionId?: unknown } = {}): Promise<ManifestSet> {
    const mode = String(source.mode || 'official');
    if (mode === 'local') return loadLocalManifest(source);
    if (mode === 'remote') return loadRemoteManifest(source);
    if (mode !== 'official') throw Object.assign(new Error('Guide source mode must be official, local, or remote'), { status: 400 });
    return loadManifests(guide, accelerator, modelServer, variant, clusterSelection);
}

guidePlanningRouter.get('/api/guide-planning/catalog', async (req, res) => {
    try {
        const { catalog, ref } = await getCatalog(req.query);
        return res.json({ repository: GITHUB_REPOSITORY, ref, guides: catalog });
    } catch (error) {
        return res.status(502).json({ error: error instanceof Error ? error.message : 'Could not load official llm-d guide catalog' });
    }
});

guidePlanningRouter.post('/api/guide-planning/prepare', async (req, res) => {
    try {
        const { guide, accelerator, modelServer, guideVariant = '' } = req.body || {};
        if (![guide, accelerator, modelServer].every((value) => typeof value === 'string' && value.trim())) {
            return res.status(400).json({ error: 'Guide, accelerator and model server are required' });
        }
        const source = await loadManifests(guide, accelerator, modelServer, String(guideVariant), req.body);
        return res.json({ ready: true, commit: source.source.commit });
    } catch {
        // A prefetch failure is non-fatal; an explicit plan will retry it.
        return res.status(502).json({ error: 'Guide preparation failed; retry when generating configuration' });
    }
});

guidePlanningRouter.post('/api/guide-planning/plan', async (req, res) => {
    const started = performance.now();
    const timings: Record<string, number> = {};
    const measure = async <T,>(name: string, work: () => Promise<T>): Promise<T> => {
        const began = performance.now();
        try { return await work(); }
        finally { timings[name] = Math.round(performance.now() - began); }
    };
    const timingHeader = () => {
        timings.total = Math.round(performance.now() - started);
        res.setHeader('Server-Timing', Object.entries(timings).map(([name, duration]) => `${name};dur=${duration}`).join(', '));
    };
    try {
        const guide = String(req.body?.guide || '').trim();
        const accelerator = String(req.body?.accelerator || '').trim();
        const modelServer = String(req.body?.modelServer || '').trim();
        const model = String(req.body?.model || '').trim();
        if (!guide || !accelerator || !modelServer || !model) return res.status(400).json({ error: 'Guide, accelerator, model server, and model are required' });
        const environment = req.body?.environment || {};
        const clusterSessionId = String(req.body?.clusterSessionId || '').trim();
        const kubernetesMode = ['auto', 'required', 'disabled'].includes(environment.kubernetesMode)
            ? environment.kubernetesMode
            : 'auto';
        const remote = environment.mode === 'remote'
            ? await discoverRemoteEnvironment(environment.target, environment.credentials)
            : null;
        const sessionKubeconfig = remote || kubernetesMode === 'disabled' || !clusterSessionId
            ? null
            : clusterSessionKubeconfig(clusterSessionId);
        if (!remote && kubernetesMode !== 'disabled' && clusterSessionId && !sessionKubeconfig) {
            throw Object.assign(new Error('The selected cluster session is no longer active'), { status: 409 });
        }
        const guideSource = req.body?.source && typeof req.body.source === 'object' ? req.body.source : { mode: 'official' };
        const guideVariant = String(req.body?.guideVariant || '').trim();
        const detectedCluster = await measure('cluster_checks', () => kubernetesMode === 'disabled' ? Promise.resolve(null) : remote ? Promise.resolve(remote.cluster) : discoverLocalCluster(sessionKubeconfig));
        if (kubernetesMode === 'required' && !detectedCluster?.preflightPassed) {
            throw Object.assign(new Error('No accessible Kubernetes cluster was found in the selected machine\'s current kubectl context'), { status: 400 });
        }
        const cluster = kubernetesMode === 'disabled' ? null : detectedCluster ?? null;
        // Pick the Guide variant from the cluster's real hardware, not a stale
        // client value, so an Intel variant never renders its gpu.intel.com DRA
        // claim against an NVIDIA (extended-resource) cluster.
        const resolvedAccelerator = resolveAcceleratorVariant(accelerator, cluster);
        const manifestSet = await measure('guide_source', () => loadSelectedManifests(guide, resolvedAccelerator, modelServer, guideSource, guideVariant, req.body));
        const detectedMachine = remote?.machine || machineProfile(!detectedCluster);
        const machine = cluster?.accelerator
            ? { ...detectedMachine, accelerator: cluster.accelerator }
            : detectedMachine;
        const renderingStarted = performance.now();
        const documents: any[] = [];
        yaml.loadAll(manifestSet.rendered, (document) => { if (document) documents.push(document); });
        const planned = planDocuments(documents, model, machine, cluster, { ...(req.body || {}), accelerator: resolvedAccelerator });
        if (resolvedAccelerator !== accelerator) {
            planned.validation.warnings.push(`Guide accelerator variant corrected from "${accelerator}" to "${resolvedAccelerator}" to match the selected cluster's hardware.`);
        }
        // Custom patches are applied only to this request's in-memory documents, strictly
        // after the standard planning pass, and are never written to the guide catalog or
        // any file on disk — so guides/plans that don't attach customPatches (including
        // Evaluation runs of other guides) are completely unaffected.
        const customPatches = Array.isArray(req.body?.customPatches) ? req.body.customPatches : [];
        const appliedCustomPatches = customPatches.length
            ? applyCustomPatches(planned.documents, customPatches, planned.validation.errors, planned.validation.warnings)
            : [];
        if (customPatches.length) {
            planned.validation.status = planned.validation.errors.length ? 'invalid' : planned.validation.warnings.length ? 'warnings' : 'valid';
        }
        planned.summary.requestedAccelerators = validateManifestCapacity(planned.documents, planned.summary.availableAccelerators, planned.summary.resourceBasis, planned.validation.errors);
        planned.validation.status = planned.validation.errors.length ? 'invalid' : planned.validation.warnings.length ? 'warnings' : 'valid';
        const deployment = deploymentContract(planned.documents, guide);
        const plannedYaml = planned.documents.map((document) => yaml.dump(document, { noRefs: true, lineWidth: 120 })).join('---\n');
        let deploymentBundle;
        if (!planned.validation.errors.length) {
            const guideSettings = normalizeGuideSettings(guide, guideVariant, req.body?.guideSettings);
            const servers = planned.documents.filter(document => document.kind === 'Deployment')
                .flatMap(document => document.spec?.template?.spec?.containers || []).filter(container => container.name === 'modelserver');
            const blockSizes = servers.map(container => runtimeArgument(container, 'block-size')).filter(value => value != null).map(Number);
            if (guide === 'precise-prefix-cache-routing' && (blockSizes.length !== servers.length || new Set(blockSizes).size !== 1)) throw new Error('Precise routing requires matching block sizes on all model servers.');
            const sourcePath = (relative: string) => manifestSet.localRoot
                ? path.join(manifestSet.localRoot, relative)
                : `https://github.com/${manifestSet.source.repository}//${relative}?ref=${manifestSet.source.commit}`;
            deploymentBundle = await measure('bundle', () => buildGuideDeploymentBundle({
                guide, source: manifestSet.source, model, blockSize: blockSizes[0], routerValues: guideSettings.routerValues,
                readSource: async (relative) => manifestSet.localRoot
                    ? fs.promises.readFile(sourcePath(relative), 'utf8')
                    : fetchText(githubContentUrl(manifestSet.source.repository, relative, manifestSet.source.commit), 'application/vnd.github.raw+json'),
                renderSource: async (relative) => (await execFileAsync('kubectl', ['kustomize', sourcePath(relative)], { encoding: 'utf8', timeout: 60_000, maxBuffer: 16 * 1024 * 1024 })).stdout,
            }));
        }
        const status = cluster?.preflightPassed && !planned.validation.errors.length ? 'VERIFIED' : 'PLANNED';
        timings.render = Math.round(performance.now() - renderingStarted);
        timingHeader();
        return res.json({
            status,
            timings,
            source: { ...manifestSet.source, guide, accelerator: resolvedAccelerator, modelServer: manifestSet.modelServer || modelServer, variant: manifestSet.variant, files: manifestSet.files.map((file) => file.path) },
            environment: { machineMode: environment.mode === 'remote' ? 'remote' : 'current', kubernetesMode, kubernetesDetected: Boolean(detectedCluster?.preflightPassed), clusterSessionId: clusterSessionId || null },
            baseManifest: manifestSet.files.map((file) => ({ path: file.path, content: file.content })),
            machineProfile: machine,
            clusterProfile: cluster,
            modelConfig: planned.summary.modelEstimate,
            planningPatch: planned.planningPatch,
            appliedCustomPatches,
            plannedDeployment: planned.validation.errors.length ? null : { format: 'yaml', content: plannedYaml },
            deploymentBundle,
            deployment,
            validation: planned.validation,
            summary: planned.summary,
        });
    } catch (error) {
        const status = typeof error === 'object' && error && 'status' in error ? Number((error as JsonRecord).status) : 500;
        const message = error instanceof Error ? error.message : 'Guide planning failed';
        console.error('[Guide Planning]', message);
        timingHeader();
        return res.status(status >= 400 && status < 600 ? status : 500).json({ error: message });
    }
});
