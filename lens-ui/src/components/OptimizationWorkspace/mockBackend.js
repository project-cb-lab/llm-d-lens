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

// =============================================================================
// MOCK BACKEND for the Deploy Workflow (Optimization Workspace)
// -----------------------------------------------------------------------------
// This module fakes the OptimalBench (llm-d-bench) Python/FastAPI control plane
// so the end-to-end deploy flow can be demonstrated inside Prism BEFORE the real
// React -> Express BFF -> FastAPI wiring exists.
//
// EVERY function here is a stand-in. The `MOCK_REGISTRY` at the bottom lists what
// each step fakes and which real OptimalBench module would eventually back it, so
// reviewers can see exactly where the seams are. When the BFF proxy lands, replace
// the bodies below with fetch('/api/control-plane/...') calls; the return shapes
// are intentionally modelled on OptimalBench's dataclasses.
// =============================================================================

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Shared mock model catalog (mirrors OptimalBench core/models MODEL_SPECS keys). */
export const MOCK_MODELS = [
  'Qwen/Qwen3-0.6B',
  'meta-llama/Llama-3.1-8B-Instruct',
  'meta-llama/Llama-3.1-70B-Instruct',
  'deepseek-ai/DeepSeek-V3',
];

export const MOCK_ACCELERATORS = [
  'Intel Gaudi 3',
  'Intel Gaudi 2',
  'Intel Data Center GPU Max 1550',
  'Intel Data Center GPU Max 1100',
  'Intel Data Center GPU Flex 170',
  'NVIDIA H100',
];

export const MOCK_MACHINE_TYPES = [
  { id: 'xeon6-xpu-8', label: 'Intel Xeon 6 XPU Node', detail: '192 vCPU · 2 TB RAM · up to 8 cards', maxCards: 8 },
  { id: 'xeon5-xpu-8', label: 'Intel Xeon 5 XPU Node', detail: '128 vCPU · 1 TB RAM · up to 8 cards', maxCards: 8 },
  { id: 'xeon4-xpu-4', label: 'Intel Xeon 4 XPU Node', detail: '96 vCPU · 768 GB RAM · up to 4 cards', maxCards: 4 },
];

export const CANDIDATE_SOURCES = [
  { id: 'aic', family: 'Predictive', label: 'AIC Prediction', description: 'OptimalBench AIConfigurator ranked predictions' },
  { id: 'baseline', family: 'Search-based', label: 'Optimized Baseline', description: 'Compare Search aggregated TP × replica space' },
  { id: 'pd', family: 'Search-based', label: 'P/D Disaggregation', description: 'Compare Search prefill/decode space' },
  { id: 'epd', family: 'Search-based', label: 'E/P/D Disaggregation', description: 'Compare Search encode/prefill/decode space' },
  { id: 'tiered-cache', family: 'Search-based', label: 'Tiered Prefix Cache', description: 'Compare Search GPU and host cache space' },
  { id: 'historical', family: 'Existing / User', label: 'Historical Example', description: 'Mock candidate for exploring the optimization workflow' },
  { id: 'manual', family: 'Existing / User', label: 'Manual Configuration', description: 'AIC estimate from typed topology or experiment YAML' },
];

// Static per-accelerator $/hr used only for the mock TCO math (real OptimalBench
// keeps this in GPU_SPECS[...]['cost_per_hour']; there is NO real TCO engine yet).
const MOCK_HOURLY_COST = {
  'Intel Gaudi 3': 2.8,
  'Intel Gaudi 2': 1.9,
  'Intel Data Center GPU Max 1550': 2.4,
  'Intel Data Center GPU Max 1100': 1.8,
  'Intel Data Center GPU Flex 170': 0.9,
  'NVIDIA H100': 3.9,
};

// -----------------------------------------------------------------------------
// STEP 2 — Search Candidate Configurations
// Real backend: OptimalBench AIC -> PD search (_generate_pd_search_space_from_aic)
// + predictor/__init__.py analytical prediction. Here we synthesize plausible
// prefill/decode topologies and predicted metrics.
// -----------------------------------------------------------------------------
export async function searchCandidates(workload, sourceIds = [], manualConfig) {
  await delay(150);
  const seed = (workload.model || '').length + (workload.isl || 0);
  const base = 40 + (seed % 25);
  const topologyBySource = {
    aic: { prefillTp: 2, prefillReplicas: 1, decodeTp: 1, decodeReplicas: 4 },
    baseline: { prefillTp: 0, prefillReplicas: 0, decodeTp: 2, decodeReplicas: 2 },
    pd: { prefillTp: 1, prefillReplicas: 2, decodeTp: 1, decodeReplicas: 4 },
    epd: { prefillTp: 2, prefillReplicas: 2, decodeTp: 2, decodeReplicas: 2 },
    'tiered-cache': { prefillTp: 1, prefillReplicas: 1, decodeTp: 2, decodeReplicas: 2 },
    historical: { prefillTp: 2, prefillReplicas: 1, decodeTp: 2, decodeReplicas: 1 },
    manual: {
      prefillTp: Number(manualConfig?.prefillTp || 1),
      prefillReplicas: Number(manualConfig?.prefillReplicas || 1),
      decodeTp: Number(manualConfig?.decodeTp || 1),
      decodeReplicas: Number(manualConfig?.decodeReplicas || 2),
    },
  };
  return sourceIds.map((source, i) => {
    const t = topologyBySource[source] || topologyBySource.baseline;
    const gpus = t.prefillTp * t.prefillReplicas + t.decodeTp * t.decodeReplicas;
    const ttft = Math.round(base * (1 + i * 0.18) + (workload.isl || 512) / 40);
    const tpot = Math.round(9 + i * 1.6 + (workload.osl || 128) / 160);
    const throughput = Math.round(2200 - i * 210 + gpus * 40);
    const sourceMeta = CANDIDATE_SOURCES.find((item) => item.id === source);
    return {
      id: `${source}-${Date.now()}-${i}`,
      name: source === 'manual' && manualConfig?.name
        ? manualConfig.name
        : `${source}-${t.prefillTp}x${t.prefillReplicas}-${t.decodeTp}x${t.decodeReplicas}`,
      source,
      sourceLabel: sourceMeta?.label || source,
      family: sourceMeta?.family || 'Search-based',
      ...t,
      totalGpus: gpus,
      predicted: { ttftMs: ttft, tpotMs: tpot, throughputTps: throughput },
      confidence: source === 'manual' ? null : Number(Math.max(0.55, 0.91 - i * 0.05).toFixed(2)),
    };
  });
}

// -----------------------------------------------------------------------------
// STEP 3 — Benchmark & Compare
// Real backend: OptimalBench benchmark/runner.py (K8s execution) + analyzer.py.
// Here we perturb the predicted numbers to look like "measured" results.
// -----------------------------------------------------------------------------
export async function runBenchmark(deployment, candidates) {
  await delay(1800);
  return candidates.map((c) => {
    const jitter = (base) => Math.round(base * (0.9 + Math.random() * 0.25));
    return {
      candidateId: c.id,
      name: c.name,
      id: `bench-${Date.now()}-${c.id}`,
      deploymentId: deployment.id,
      deploymentName: deployment.name,
      machineType: deployment.machineType,
      accelerator: deployment.accelerator,
      createdAt: new Date().toISOString(),
      measured: {
        ttftMs: jitter(c.predicted.ttftMs),
        tpotMs: jitter(c.predicted.tpotMs),
        throughputTps: jitter(c.predicted.throughputTps),
        errorRate: Number((Math.random() * 0.4).toFixed(2)),
      },
      totalGpus: c.totalGpus,
    };
  });
}

export async function runSimulation(deployment, workload) {
  await delay(1300);
  return {
    id: `sim-${Date.now()}`,
    deploymentId: deployment.id,
    deploymentName: deployment.name,
    createdAt: new Date().toISOString(),
    scenario: `${workload.concurrency} concurrent · ${workload.isl}/${workload.osl} ISL/OSL`,
    projectedThroughputTps: Math.round(1800 + Math.random() * 700),
    saturationConcurrency: Math.round(Number(workload.concurrency) * (1.25 + Math.random() * 0.5)),
    verdict: 'Simulation completed independently of benchmark execution.',
  };
}

// -----------------------------------------------------------------------------
// STEP 5 — Performance + TCO
// Real backend: DOES NOT EXIST in OptimalBench (no TCO engine, no RateCard).
// This is a fully invented cost model: gpus x $/hr x hours, no depreciation/power.
// -----------------------------------------------------------------------------
export async function computeTco(benchmarks, accelerator, hoursPerMonth = 730) {
  await delay(900);
  return benchmarks.map((b) => {
    const benchmarkAccelerator = b.accelerator || accelerator;
    const rate = MOCK_HOURLY_COST[benchmarkAccelerator] ?? 2.0;
    const monthlyCost = Number((b.totalGpus * rate * hoursPerMonth).toFixed(0));
    const tokensPerMonth = b.measured.throughputTps * 3600 * hoursPerMonth;
    const costPerMillionTokens = Number(((monthlyCost / tokensPerMonth) * 1_000_000).toFixed(3));
    return {
      candidateId: b.candidateId,
      benchmarkId: b.id,
      deploymentId: b.deploymentId,
      machineType: b.machineType,
      accelerator: benchmarkAccelerator,
      name: b.name,
      totalGpus: b.totalGpus,
      hourlyCost: Number((b.totalGpus * rate).toFixed(2)),
      monthlyCost,
      costPerMillionTokens,
      throughputTps: b.measured.throughputTps,
      ttftMs: b.measured.ttftMs,
      tpotMs: b.measured.tpotMs,
    };
  });
}

// -----------------------------------------------------------------------------
// STEP 6 — Recommendation
// Real backend: OptimalBench advisor/LLMAdvisor (explain-only) + deterministic
// scoring. Here we deterministically pick the best cost/token that meets SLA and
// attach a canned "explanation".
// -----------------------------------------------------------------------------
export async function recommend(tcoRows, sla) {
  await delay(1100);
  const feasible = tcoRows.filter(
    (r) => r.ttftMs <= sla.ttftMs && r.tpotMs <= sla.tpotMs
  );
  const pool = feasible.length ? feasible : tcoRows;
  const winner = [...pool].sort((a, b) => a.costPerMillionTokens - b.costPerMillionTokens)[0];
  return {
    winnerId: winner.candidateId,
    ranked: [...pool].sort((a, b) => a.costPerMillionTokens - b.costPerMillionTokens),
    slaMet: feasible.some((r) => r.candidateId === winner.candidateId),
    explanation:
      `Recommended ${winner.name}: lowest cost/1M tokens ($${winner.costPerMillionTokens}) ` +
      `among configs${feasible.length ? ' that meet the SLA' : ' (no config met the SLA — closest shown)'}. ` +
      `Deterministic scoring is authoritative; this text is an advisory explanation only.`,
  };
}

// -----------------------------------------------------------------------------
// STEP 7 — Deployment
// Real backend: OptimalBench deployment/DeploymentManager (helm router +
// kustomize modelserver). Here we stream fake pod-status transitions.
// -----------------------------------------------------------------------------
export async function deploy(plan, onProgress) {
  const phases = [
    { phase: 'Rendering Helm values', pct: 15 },
    { phase: 'helm upgrade --install llm-d-router', pct: 35 },
    { phase: 'kubectl apply -k modelserver overlay', pct: 55 },
    { phase: 'Waiting for prefill pods (Running)', pct: 75 },
    { phase: 'Waiting for decode pods (Running)', pct: 90 },
    { phase: 'Gateway endpoint ready', pct: 100 },
  ];
  for (const p of phases) {
    await delay(700);
    onProgress?.(p);
  }
  return {
    id: `deploy-${Date.now()}`,
    name: plan.name,
    candidateId: plan.candidateId || null,
    candidateName: plan.candidateName || 'Standalone manual configuration',
    cluster: plan.cluster,
    machineType: plan.machineType,
    accelerator: plan.accelerator,
    nodes: Number(plan.nodes),
    gpusPerNode: Number(plan.gpusPerNode),
    replicas: Number(plan.replicas),
    createdAt: new Date().toISOString(),
    namespace: plan.namespace || 'llm-d-optimized',
    status: 'ready',
    endpoint: `http://${plan.name}.${plan.namespace || 'llm-d-optimized'}.svc.cluster.local:80`,
    pods: [
      { name: `${plan.name}-prefill-0`, status: 'Running', restarts: 0 },
      { name: `${plan.name}-decode-0`, status: 'Running', restarts: 0 },
      { name: `${plan.name}-decode-1`, status: 'Running', restarts: 0 },
    ],
  };
}

// -----------------------------------------------------------------------------
// STEP 8 — Post-deployment Validation
// Real backend: DOES NOT EXIST as a closed loop. DeploymentManager only checks
// pod readiness. This mock runs a short validation "Run" and compares to SLA,
// occasionally failing to exercise the Re-optimize branch.
// -----------------------------------------------------------------------------
export async function validateDeployment(recommendedRow, sla, { forceOutcome } = {}) {
  await delay(1500);
  const drift = 1 + (Math.random() * 0.5 - 0.15); // -15%..+35%
  const ttftMs = Math.round(recommendedRow.ttftMs * drift);
  const tpotMs = Math.round(recommendedRow.tpotMs * drift);
  let meetsSla = ttftMs <= sla.ttftMs && tpotMs <= sla.tpotMs;
  if (forceOutcome === 'pass') meetsSla = true;
  if (forceOutcome === 'fail') meetsSla = false;
  return {
    ttftMs,
    tpotMs,
    throughputTps: recommendedRow.throughputTps,
    meetsSla,
    verdict: meetsSla ? 'pass' : 'fail',
    note: meetsSla
      ? 'Live validation Run met the SLA. Deployment accepted.'
      : 'Live validation Run exceeded SLA latency. Recommend re-optimize.',
  };
}

// -----------------------------------------------------------------------------
// Registry of what is faked, surfaced in the UI so mocks are never hidden.
// -----------------------------------------------------------------------------
export const MOCK_REGISTRY = {
  'define-workload': {
    level: 'local',
    note: 'Form is real but stored only in component state (not persisted to a backend).',
    realBackend: 'OptimalBench core/models WorkloadConfig + SLATargets',
  },
  'search-candidates': {
    level: 'hybrid',
    note: 'Official llm-d guide YAML and machine/Kubernetes discovery are real. Model memory feasibility is an estimate when authoritative model metadata is unavailable.',
    realBackend: 'llm-d official guide manifests + Prism machine discovery + kubectl preflight + planning resolver',
  },
  deployment: {
    level: 'real',
    note: 'Remote SSH runs real OptimalBench custom deployment, Kubernetes readiness polling, logs, and teardown.',
    realBackend: 'OptimalBench deployment/DeploymentManager',
  },
  benchmark: {
    level: 'mock',
    note: 'Benchmark measurements are synthesized against a selected deployment.',
    realBackend: 'OptimalBench benchmark runner',
  },
  'performance-tco': {
    level: 'mock',
    note: 'TCO uses an illustrative GPU rate card; Pareto status and agent recommendation are local.',
    realBackend: 'Benchmark analyzer + net-new TCO/RateCard engine + LLM Advisor',
  },
};
