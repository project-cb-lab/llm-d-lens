// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

// Client for the backend hardware profile registry
// (GET /api/v1/hardware/capabilities). Profiles drive hardware-specific UI
// (accelerator vendor tabs, defaults) instead of hardcoded vendor lists.

import { requestJson } from '../../api/httpClient';

const BASE_PATH = '/api/v1/hardware';

export const DEFAULT_ACCESS_MODES = ['dra', 'plugin'];

export function getHardwareProfiles({ signal } = {}) {
    return requestJson(`${BASE_PATH}/capabilities`, { signal });
}

// Map registered hardware profiles to accelerator vendor tabs. Each entry keeps
// the profile id (sent as the gpu-driver `hardware` query param) and the access
// modes the profile declares. Returns the fallback list when no profiles
// resolve, and keeps disabled fallback entries (e.g. a planned vendor) that the
// profiles do not already cover.
export function acceleratorVendorsFromProfiles(profiles = [], fallback = []) {
    const vendors = [];
    for (const profile of profiles || []) {
        const id = profile?.vendor || profile?.id;
        if (!id) continue;
        const accessModes = Array.isArray(profile.access_modes) && profile.access_modes.length
            ? profile.access_modes
            : DEFAULT_ACCESS_MODES;
        vendors.push({ id, label: profile.display_name || id, disabled: false, profileId: profile.id, accessModes });
    }
    if (!vendors.length) return fallback;
    for (const entry of fallback) {
        if (entry.disabled && !vendors.some((vendor) => vendor.id === entry.id)) vendors.push(entry);
    }
    return vendors;
}

// Access modes the selected vendor supports, defaulting to dra/plugin.
export function accessModesForVendor(vendors = [], vendorId) {
    const vendor = (vendors || []).find((entry) => entry.id === vendorId);
    return Array.isArray(vendor?.accessModes) && vendor.accessModes.length ? vendor.accessModes : DEFAULT_ACCESS_MODES;
}
