// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import assert from 'node:assert/strict';
import test from 'node:test';

import { acceleratorVendorsFromProfiles, accessModesForVendor, DEFAULT_ACCESS_MODES } from './hardwareProfilesBackend.js';

const FALLBACK = [
    { id: 'intel', label: 'Intel GPU', disabled: false, accessModes: ['dra', 'plugin'] },
    { id: 'nvidia', label: 'Nvidia GPU', disabled: true, accessModes: ['dra', 'plugin'] },
];

test('profiles enable their vendor, carry access modes, and keep uncovered disabled placeholders', () => {
    const vendors = acceleratorVendorsFromProfiles(
        [{ id: 'intel-xpu', vendor: 'intel', display_name: 'Intel GPU', access_modes: ['dra', 'plugin'] }],
        FALLBACK,
    );
    assert.deepEqual(vendors, [
        { id: 'intel', label: 'Intel GPU', disabled: false, profileId: 'intel-xpu', accessModes: ['dra', 'plugin'] },
        { id: 'nvidia', label: 'Nvidia GPU', disabled: true, accessModes: ['dra', 'plugin'] },
    ]);
});

test('profiles without access modes fall back to dra/plugin', () => {
    const vendors = acceleratorVendorsFromProfiles([{ id: 'intel-xpu', vendor: 'intel', display_name: 'Intel GPU' }], FALLBACK);
    assert.deepEqual(vendors[0].accessModes, DEFAULT_ACCESS_MODES);
});

test('no profiles falls back to the static vendor list', () => {
    assert.equal(acceleratorVendorsFromProfiles([], FALLBACK), FALLBACK);
});

test('profiles without a vendor id fall back to the profile id', () => {
    const vendors = acceleratorVendorsFromProfiles([{ id: 'nvidia', display_name: 'NVIDIA GPU' }], FALLBACK);
    assert.deepEqual(vendors, [
        { id: 'nvidia', label: 'NVIDIA GPU', disabled: false, profileId: 'nvidia', accessModes: DEFAULT_ACCESS_MODES },
    ]);
});

test('accessModesForVendor returns the vendor modes or the default', () => {
    const vendors = [{ id: 'nvidia', accessModes: ['plugin'] }, { id: 'intel', accessModes: ['dra', 'plugin'] }];
    assert.deepEqual(accessModesForVendor(vendors, 'nvidia'), ['plugin']);
    assert.deepEqual(accessModesForVendor(vendors, 'intel'), ['dra', 'plugin']);
    assert.deepEqual(accessModesForVendor(vendors, 'missing'), DEFAULT_ACCESS_MODES);
    assert.deepEqual(accessModesForVendor([], 'nvidia'), DEFAULT_ACCESS_MODES);
});
