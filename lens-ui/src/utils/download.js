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

function filenameFromContentDisposition(disposition) {
    if (!disposition) return null;
    const star = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    if (star) {
        try {
            return decodeURIComponent(star[1]);
        } catch {
            return star[1];
        }
    }
    const plain = /filename="?([^";]+)"?/i.exec(disposition);
    return plain ? plain[1] : null;
}

/**
 * Fetch a file from the Prism backend and trigger a browser download.
 *
 * Adds the GitHub access token header when one is present so downloads work
 * behind the same auth gate as the rest of the API. The saved filename is
 * taken from the response's Content-Disposition header, falling back to
 * `fallbackName` (or "download").
 *
 * @param {string} url relative or absolute download URL
 * @param {{fallbackName?: string}} [options]
 * @returns {Promise<string>} the filename the file was saved as
 */
export async function downloadFile(url, { fallbackName } = {}) {
    const response = await fetch(url, { credentials: 'include' });
    if (!response.ok) {
        let payload = null;
        try {
            payload = await response.json();
        } catch {
            payload = null;
        }
        const detail = payload?.detail || payload?.error || payload?.message;
        let message = `${response.status} ${response.statusText}`;
        if (Array.isArray(detail)) {
            message = detail.map((item) => item.msg || item.message || JSON.stringify(item)).join('; ');
        } else if (typeof detail === 'string') {
            message = detail;
        }
        throw new Error(message);
    }
    const blob = await response.blob();
    const filename = filenameFromContentDisposition(response.headers.get('content-disposition'))
        || fallbackName
        || 'download';
    downloadBlob(blob, filename);
    return filename;
}

/** Trigger a local download; keep the URL alive until the browser consumes it. */
export function downloadBlob(blob, filename) {
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    try {
        anchor.href = objectUrl;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
    } finally {
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    }
}
