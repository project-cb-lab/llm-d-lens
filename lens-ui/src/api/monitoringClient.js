import { requestJson } from './httpClient.js';

export async function requestMonitoringJson(path, options = {}) {
    try {
        return await requestJson(path, options, { fallback: null });
    } catch (error) {
        if (error.status && !error.code) error.code = `HTTP_${error.status}`;
        throw error;
    }
}
