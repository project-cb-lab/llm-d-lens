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

import { requestJson as sharedRequestJson } from '../../api/httpClient';

function apiError(payload, fallback) {
    const detail = payload?.detail || payload?.error || payload?.message;
    if (Array.isArray(detail)) {
        return detail.map((item) => item.msg || item.message || JSON.stringify(item)).join('; ');
    }
    return typeof detail === 'string' ? detail : fallback;
}

export async function requestJson(url, options) {
    return sharedRequestJson(url, options, {
        fallback: null,
        errorFactory: (payload, response) => new Error(apiError(payload, `${response.status} ${response.statusText}`)),
    });
}
