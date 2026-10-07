// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

// Consume the Python hardware profile registry (GET /api/v1/hardware/capabilities)
// so gateway planning resolves accelerator identity from registered profiles
// instead of hardcoded vendor strings. Results are cached briefly; on failure
// callers keep their previous profile snapshot (or fall back to their own
// defaults) rather than blocking.

import { fetchJsonWithTimeout } from './http';
import { internalHeadersFor } from './internalAuth.ts';

const BACKEND_URL = (
    process.env.LLM_D_DEPLOY_API_URL ||
    process.env.SIMULATION_API_URL ||
    process.env.CONFIGURATION_API_URL ||
    'http://127.0.0.1:8081'
).replace(/\/$/, '');
const REQUEST_TIMEOUT_MS = Number(process.env.HARDWARE_PROFILES_TIMEOUT_MS || 5_000);
const CACHE_TTL_MS = Number(process.env.HARDWARE_PROFILES_CACHE_TTL_MS || 30_000);

export type HardwareProfile = {
    id?: string;
    display_name?: string;
    vendor?: string;
    accelerator_keys?: string[];
    device_classes?: string[];
    planning?: { aic_system_patterns?: string[] };
};

let cache: { at: number; profiles: HardwareProfile[] } | null = null;

export async function loadHardwareProfiles(): Promise<HardwareProfile[]> {
    const now = Date.now();
    if (cache && now - cache.at < CACHE_TTL_MS) return cache.profiles;
    const path = '/api/v1/hardware/capabilities';
    try {
        const headers = new Headers();
        for (const [name, value] of Object.entries(internalHeadersFor('GET', path))) headers.set(name, value);
        const { response, payload } = await fetchJsonWithTimeout(
            `${BACKEND_URL}${path}`, { headers }, REQUEST_TIMEOUT_MS,
        );
        if (!response.ok) return cache?.profiles || [];
        const profiles = Array.isArray(payload?.profiles) ? (payload.profiles as HardwareProfile[]) : [];
        cache = { at: now, profiles };
        return profiles;
    } catch {
        return cache?.profiles || [];
    }
}

const AIC_ACCELERATORS = new Set(['xpu', 'cuda']);

// AIConfigurator `accelerator` value for a system name, from the registered
// profiles' planning patterns; null when nothing matches.
export function aicAcceleratorForSystem(
    systemName: string,
    profiles: HardwareProfile[] = [],
): 'xpu' | 'cuda' | null {
    const name = String(systemName || '');
    if (!name) return null;
    for (const profile of profiles) {
        const patterns = profile?.planning?.aic_system_patterns || [];
        const matched = patterns.some((pattern) => {
            try { return new RegExp(pattern, 'i').test(name); } catch { return false; }
        });
        if (!matched) continue;
        const key = (profile.accelerator_keys || [])
            .map((value) => String(value).toLowerCase())
            .find((value) => AIC_ACCELERATORS.has(value));
        if (key) return key as 'xpu' | 'cuda';
    }
    return null;
}

// True when a DRA device class belongs to a registered profile.
export function isKnownDeviceClass(deviceClass: string, profiles: HardwareProfile[] = []): boolean {
    const value = String(deviceClass || '');
    return value !== '' && profiles.some((profile) => (profile.device_classes || []).includes(value));
}
