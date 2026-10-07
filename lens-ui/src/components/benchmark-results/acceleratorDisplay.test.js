// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import assert from 'node:assert/strict';
import test from 'node:test';

import { acceleratorValue, acceleratorDisplayLabel, utilizationLabel, acceleratorVariantForHardware, runtimeImageForHardware, DEFAULT_RUNTIME_IMAGES } from './acceleratorDisplay.js';

test('reads the accelerator profile from common run shapes', () => {
  assert.equal(acceleratorValue({ metrics: { accelerator_profile: 'intel-xpu' } }), 'intel-xpu');
  assert.equal(acceleratorValue({ configuration: { accelerator: 'cuda' } }), 'cuda');
  assert.equal(acceleratorValue({ resource_snapshot: { accelerator: { model: 'B60' } } }).model, 'B60');
});

test('labels known accelerators and leaves unknown ones neutral', () => {
  assert.equal(acceleratorDisplayLabel('intel-xpu'), 'XPU');
  assert.equal(acceleratorDisplayLabel('nvidia-cuda'), 'GPU');
  assert.equal(acceleratorDisplayLabel({ model: 'B60 XPU' }), 'XPU');
  assert.equal(acceleratorDisplayLabel(''), null);
});

test('utilization label falls back to the neutral wording', () => {
  assert.equal(utilizationLabel('intel-xpu'), 'XPU utilization');
  assert.equal(utilizationLabel('cuda'), 'GPU utilization');
  assert.equal(utilizationLabel(null), 'GPU / XPU utilization');
});

test('picks the per-vendor runtime image from the cluster hardware', () => {
  const gpu = { accelerators: [{ id: 'nvidia' }] };
  const xpu = { accelerators: [{ id: 'intel' }] };
  assert.equal(acceleratorVariantForHardware(gpu), 'gpu');
  assert.equal(acceleratorVariantForHardware(xpu), 'xpu');
  assert.equal(runtimeImageForHardware(gpu), DEFAULT_RUNTIME_IMAGES.gpu);
  assert.equal(runtimeImageForHardware(xpu), DEFAULT_RUNTIME_IMAGES.xpu);
  // A GPU cluster never falls back to the XPU image; unknown hardware is neutral.
  assert.notEqual(runtimeImageForHardware(gpu), DEFAULT_RUNTIME_IMAGES.xpu);
  assert.equal(runtimeImageForHardware({ accelerators: [] }), null);
  assert.equal(runtimeImageForHardware(null), null);
});
