// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

// Map a benchmark run's accelerator profile/runtime to a display label so chart
// titles describe the hardware that actually ran (XPU, GPU, or a neutral
// fallback) instead of a hardcoded "GPU / XPU".

export function acceleratorValue(record = {}) {
  return (
    record.metrics?.accelerator_profile ||
    record.accelerator_profile ||
    record.configuration?.accelerator ||
    record.spec?.accelerator ||
    record.deployment_configuration?.content?.accelerator ||
    record.resource_snapshot?.accelerator ||
    null
  );
}

export function acceleratorDisplayLabel(value) {
  const text = typeof value === 'string' ? value : value?.model || value?.name || '';
  const normalized = String(text).toLowerCase();
  if (normalized.includes('xpu') || normalized.includes('intel')) return 'XPU';
  if (normalized.includes('cuda') || normalized.includes('nvidia')) return 'GPU';
  return null;
}

export function utilizationLabel(value) {
  const label = acceleratorDisplayLabel(value);
  return label ? `${label} utilization` : 'GPU / XPU utilization';
}

// Per-vendor llm-d model-server image repositories. The repository and version
// are owned by the hardware profile and applied by the backend, so only the
// repository is tracked here; the default follows the selected cluster's hardware.
export const DEFAULT_RUNTIME_IMAGES = {
  gpu: 'ghcr.io/llm-d/llm-d-cuda',
  xpu: 'ghcr.io/llm-d/llm-d-xpu',
};

// Every model-server image family the hardware profiles own (older llm-d images
// and upstream vLLM ones). The backend normalizes any of these to the resolved
// profile image, so none of them may be mistaken for a user's custom image.
export const MANAGED_RUNTIME_IMAGE_REPOSITORIES = [
  'ghcr.io/llm-d/llm-d-cuda',
  'ghcr.io/llm-d/llm-d-xpu',
  'ghcr.io/llm-d/llm-d-rocm',
  'docker.io/vllm/vllm-openai',
  'docker.io/vllm/vllm-openai-xpu',
  'docker.io/vllm/vllm-openai-rocm',
];

export function runtimeImageRepository(image) {
  return String(image || '').split('@')[0].split(':')[0];
}

// True when an image belongs to a managed model-server family (ignoring any tag),
// so hardware realignment never clobbers a custom image.
export function isDefaultRuntimeImage(image) {
  return MANAGED_RUNTIME_IMAGE_REPOSITORIES.includes(runtimeImageRepository(image));
}

// The Guide's upstream variant id for a cluster's discovered hardware: `gpu`
// (NVIDIA) or `xpu` (Intel). Reads the cluster overview `hardware.accelerators`
// profile ids so the deploy configuration cannot stay on the wrong vendor.
export function acceleratorVariantForHardware(hardware) {
  const ids = (hardware?.accelerators || [])
    .map((entry) => String(entry?.id || entry || '').toLowerCase())
    .filter(Boolean);
  if (!ids.length) return null;
  const has = (needle) => ids.some((id) => id.includes(needle));
  if (has('nvidia') && !has('intel')) return 'gpu';
  if (has('intel') && !has('nvidia')) return 'xpu';
  return null;
}

// The default model-server image for a cluster's hardware, or null when the
// vendor is unknown (caller keeps its current image).
export function runtimeImageForHardware(hardware) {
  const variant = acceleratorVariantForHardware(hardware);
  return variant ? DEFAULT_RUNTIME_IMAGES[variant] : null;
}
