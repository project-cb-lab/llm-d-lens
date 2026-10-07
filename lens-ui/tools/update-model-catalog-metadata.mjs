import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, writeFile } from 'node:fs/promises';

const execFileAsync = promisify(execFile);
const catalogPath = new URL('../src/data/modelCatalog.js', import.meta.url);
const outputPath = new URL('../src/data/modelCatalogMetadata.json', import.meta.url);
const contextKeys = [
    'max_position_embeddings',
    'max_sequence_length',
    'max_seq_len',
    'max_context_length',
    'context_length',
    'model_max_length',
    'n_positions',
];

function contextLength(value) {
    if (!value || typeof value !== 'object') return null;
    for (const key of contextKeys) {
        const candidate = Number(value[key]);
        if (Number.isSafeInteger(candidate) && candidate > 0 && candidate < 10_000_000) return candidate;
    }
    for (const child of Object.values(value)) {
        const found = contextLength(child);
        if (found) return found;
    }
    return null;
}

async function curlJson(url) {
    const { stdout } = await execFileAsync('curl', ['--fail', '--silent', '--show-error', '--location', '--max-time', '45', url]);
    return JSON.parse(stdout);
}

async function metadataFor(repository) {
    const escapedRepository = repository.split('/').map(encodeURIComponent).join('/');
    const api = await curlJson(`https://huggingface.co/api/models/${escapedRepository}?expand[]=safetensors`);
    let config = {};
    try {
        config = await curlJson(`https://huggingface.co/${escapedRepository}/raw/main/config.json`);
    } catch {
        // Some repositories do not publish a config file. Keep their catalog entry usable.
    }
    const totalBytes = Number(api?.safetensors?.total);
    return {
        weightGiB: Number.isFinite(totalBytes) && totalBytes > 0 ? Number((totalBytes / 1024 ** 3).toFixed(1)) : null,
        contextLength: contextLength(config),
    };
}

const source = await readFile(catalogPath, 'utf8');
const repositories = [...source.matchAll(/(?:`|\n)(?:\d{4}-\d{2}-\d{2})\|([^|\n]+)/g)].map((match) => match[1]);
const entries = await Promise.all(repositories.map(async (repository) => {
    try {
        return [repository, await metadataFor(repository)];
    } catch (error) {
        console.warn(`Unable to fetch ${repository}: ${error.message}`);
        return [repository, { weightGiB: null, contextLength: null }];
    }
}));

await writeFile(outputPath, `${JSON.stringify(Object.fromEntries(entries), null, 2)}\n`);