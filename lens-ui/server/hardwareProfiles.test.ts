// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import assert from 'node:assert/strict';
import test from 'node:test';

import { aicAcceleratorForSystem, isKnownDeviceClass, type HardwareProfile } from './hardwareProfiles.ts';

const INTEL: HardwareProfile = {
    id: 'intel-xpu',
    accelerator_keys: ['xpu', 'intel_gpu'],
    device_classes: ['gpu.intel.com'],
    planning: { aic_system_patterns: ['bmg|max_|xpu|b60|pvc'] },
};
const NVIDIA: HardwareProfile = {
    id: 'nvidia',
    accelerator_keys: ['cuda', 'nvidia'],
    device_classes: ['gpu.nvidia.com'],
    planning: { aic_system_patterns: ['h100|h200|b200|nvidia'] },
};

test('resolves the AIC accelerator from registered profile planning patterns', () => {
    assert.equal(aicAcceleratorForSystem('BMG 1550', [INTEL, NVIDIA]), 'xpu');
    assert.equal(aicAcceleratorForSystem('H100', [INTEL, NVIDIA]), 'cuda');
    assert.equal(aicAcceleratorForSystem('unknown-system', [INTEL, NVIDIA]), null);
    assert.equal(aicAcceleratorForSystem('', [INTEL]), null);
});

test('device classes are recognized only when registered', () => {
    assert.equal(isKnownDeviceClass('gpu.intel.com', [INTEL]), true);
    assert.equal(isKnownDeviceClass('gpu.nvidia.com', [INTEL]), false);
    assert.equal(isKnownDeviceClass('gpu.nvidia.com', [INTEL, NVIDIA]), true);
    assert.equal(isKnownDeviceClass('', [INTEL, NVIDIA]), false);
});
