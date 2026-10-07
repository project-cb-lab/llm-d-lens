// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

// Access modes are a fixed, hardware-independent vocabulary (how a device is
// exposed to Kubernetes), so the labels stay generic instead of naming one
// vendor's plugin. Which modes a given accelerator offers comes from the
// backend capabilities payload, never from this map.
const ACCESS_MODE_LABELS = {
    dra: 'DRA',
    plugin: 'Device plugin',
};

export function accessModeLabel(mode) {
    if (!mode) return 'Unknown';
    return ACCESS_MODE_LABELS[mode] || String(mode).replace(/_/g, ' ');
}
