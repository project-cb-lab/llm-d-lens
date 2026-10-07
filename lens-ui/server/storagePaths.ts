import os from 'node:os';
import path from 'node:path';

/** Same selection contract as llm_d_bench.utils.paths; no I/O or migration. */
export function storagePath(area: 'data' | 'cache' | 'log' | 'scratch' | 'runtime', parts: string[] = [], env: NodeJS.ProcessEnv = process.env): string {
    const home = os.homedir();
    const defaults = {
        data: path.join(env.XDG_DATA_HOME || path.join(home, '.local/share'), 'lens'),
        cache: path.join(env.XDG_CACHE_HOME || path.join(home, '.cache'), 'lens'),
        log: path.join(env.XDG_STATE_HOME || path.join(home, '.local/state'), 'lens/logs'),
        scratch: '', runtime: '',
    };
    if (area === 'scratch') defaults.scratch = path.join(storagePath('cache', [], env), 'tmp');
    if (area === 'runtime') defaults.runtime = env.XDG_RUNTIME_DIR ? path.join(env.XDG_RUNTIME_DIR, 'lens') : path.join(storagePath('scratch', [], env), 'runtime');
    if (parts.some(part => path.isAbsolute(part) || part.split(/[\\/]/).includes('..'))) throw new Error('Storage path must be relative without traversal');
    const expand = (value: string) => value === '~' ? home : value.startsWith('~/') ? path.join(home, value.slice(2)) : value;
    return path.resolve(expand(env[`LENS_${area.toUpperCase()}_DIR`] || defaults[area]), ...parts);
}
