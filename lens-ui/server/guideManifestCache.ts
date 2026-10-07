import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

// Only commit-addressed upstream source manifests belong here. Cluster state,
// planned overrides and mutable local working trees must never use this cache.
export function createGuideManifestCache<T>(limit = 24, options: {
    directory?: string;
    validate?: (value: unknown) => value is T;
} = {}) {
    const entries = new Map<string, Promise<T>>();
    return (key: string | null, load: () => Promise<T>): Promise<T> => {
        if (key === null) return load();
        const existing = entries.get(key);
        if (existing) return existing;
        const pending = (async () => {
            const file = options.directory ? path.join(options.directory, `${createHash('sha256').update(key).digest('hex')}.json`) : null;
            if (file) {
                try {
                    const value: unknown = JSON.parse(await fs.readFile(file, 'utf8'));
                    if (!options.validate || options.validate(value)) return value as T;
                } catch { /* A missing or corrupt cache is repaired by the source load. */ }
            }
            const value = await load();
            if (file) {
                const temporary = `${file}.${randomUUID()}.tmp`;
                try {
                    await fs.mkdir(options.directory!, { recursive: true, mode: 0o700 });
                    await fs.writeFile(temporary, JSON.stringify(value), { mode: 0o600 });
                    await fs.rename(temporary, file);
                } catch {
                    await fs.rm(temporary, { force: true }).catch(() => {});
                    // Cache persistence must not turn a successful source load into an error.
                }
            }
            return value;
        })().catch((error) => {
            if (entries.get(key) === pending) entries.delete(key);
            throw error;
        });
        entries.set(key, pending);
        if (entries.size > limit) entries.delete(entries.keys().next().value!);
        return pending;
    };
}
