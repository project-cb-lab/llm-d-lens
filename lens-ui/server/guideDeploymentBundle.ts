import crypto from 'node:crypto';
import fs from 'node:fs';
import yaml from 'js-yaml';

/* Helm values are schema-dynamic and include nested YAML plugin files. */
/* eslint-disable @typescript-eslint/no-explicit-any */
type RecordValue = Record<string, any>;
/* The pinned stack profile is the single source of truth for llm-d component
 * versions (llm_d_bench/versions/llm_d_stack.yaml), shared with the Python
 * backend so the planner and the renderer never disagree. */
const stackProfile = yaml.load(fs.readFileSync(new URL('../llm_d_bench/versions/llm_d_stack.yaml', import.meta.url), 'utf8')) as RecordValue;
export const ROUTER_CHART_VERSION = String(stackProfile.llm_d_router);
export const ROUTER_DISAGG_SIDECAR_IMAGE = `ghcr.io/llm-d/llm-d-router-disagg-sidecar:${ROUTER_CHART_VERSION}`;

/* Model-server images are owned by the hardware profiles (repository and
 * version), resolved from the profile registry directory so the planner never
 * branches on a vendor or hardcodes a file path. */
function loadModelServerImages(): Record<string, string> {
    const directory = new URL('../llm_d_bench/hardware/profiles/', import.meta.url);
    const images: Record<string, string> = {};
    for (const entry of fs.readdirSync(directory)) {
        if (!entry.endsWith('.json')) continue;
        const profile = JSON.parse(fs.readFileSync(new URL(entry, directory), 'utf8')) as RecordValue;
        const image = profile?.deployment?.runtime_image;
        if (typeof image === 'string' && image) images[image.split('@')[0].split(':')[0]] = image;
    }
    return images;
}
const modelServerImages = loadModelServerImages();

/* The deployed manifest must carry the model-server image the backend pins, so
 * the configuration validates. A repository a profile owns is replaced by that
 * profile's full image; a custom image passes through unchanged. */
export function pinModelServerImage(image: string): string {
    const repository = image.split('@')[0].split(':')[0];
    return modelServerImages[repository] ?? image;
}
const routerPaths = {
    'optimized-baseline': 'optimized-baseline.values.yaml',
    'pd-disaggregation': 'pd-disaggregation.values.yaml',
    'tiered-prefix-cache': 'tiered-prefix-cache-cpu.values.yaml',
    'precise-prefix-cache-routing': 'precise-prefix-cache-routing.values.yaml',
};
const asset = (name: string, content: string) => ({ name, content, checksum: `sha256:${crypto.createHash('sha256').update(content).digest('hex')}` });
const mapping = (value: any): value is RecordValue => Boolean(value && typeof value === 'object' && !Array.isArray(value));
function valuesYaml(content: string): RecordValue {
    const parsed = yaml.load(content);
    if (!mapping(parsed)) throw new Error('Router values must be a YAML mapping.');
    return parsed;
}
function merge(base: RecordValue, overlay: RecordValue): RecordValue {
    for (const [key, value] of Object.entries(overlay)) {
        if (['__proto__', 'constructor', 'prototype'].includes(key)) throw new Error('Invalid router values key.');
        base[key] = mapping(value) && mapping(base[key]) ? merge(base[key], value) : value;
    }
    return base;
}

export async function buildGuideDeploymentBundle({ guide, source, model, blockSize, routerValues = '', readSource, renderSource }: {
    guide: string; source: RecordValue; model: string; blockSize?: number; routerValues?: string;
    // eslint-disable-next-line no-unused-vars
    readSource: (path: string) => Promise<string>; renderSource: (path: string) => Promise<string>;
}) {
    const routerPath = routerPaths[guide];
    if (!routerPath) throw new Error('This Guide does not have a supported deployment bundle.');
    const [baseText, guideText] = await Promise.all([
        readSource('guides/recipes/router/base.values.yaml'),
        readSource(`guides/${guide}/router/${routerPath}`),
    ]);
    const values = merge(valuesYaml(baseText), valuesYaml(guideText));
    if (routerValues.trim()) merge(values, valuesYaml(routerValues));
    if (guide === 'precise-prefix-cache-routing') {
        const epp = values.router?.epp;
        const key = epp?.pluginsConfigFile;
        const plugins = key && typeof epp.pluginsCustomConfig?.[key] === 'string' ? valuesYaml(epp.pluginsCustomConfig[key]) : null;
        const tokenProducer = plugins?.plugins?.find(item => item.type === 'token-producer');
        const index = plugins?.plugins?.find(item => ['precise-prefix-cache-producer', 'precise-prefix-cache-scorer'].includes(item.type));
        if (!tokenProducer || !index) throw new Error('Precise routing requires token-producer and precise-prefix-cache plugins.');
        tokenProducer.parameters = { ...tokenProducer.parameters, modelName: model };
        if (blockSize == null || !Number.isSafeInteger(blockSize) || blockSize <= 0) throw new Error('Precise routing requires an explicit or inherited model-server block size.');
        index.parameters ||= {};
        index.parameters.tokenProcessorConfig = { ...index.parameters.tokenProcessorConfig, blockSizeTokens: blockSize };
        epp.pluginsCustomConfig[key] = yaml.dump(plugins, { lineWidth: -1 });
        if (Number(epp.replicas ?? 1) !== 1) throw new Error('Precise token-load routing currently requires one EPP replica.');
    }
    const resources: ReturnType<typeof asset>[] = [];
    let calibration: ReturnType<typeof asset>[] | undefined;
    if (guide === 'precise-prefix-cache-routing') {
        const [rendered, script, jobTemplate] = await Promise.all([
            renderSource('guides/precise-prefix-cache-routing/render'),
            readSource('guides/recipes/router/calibration/calibrate.sh'),
            readSource('guides/recipes/router/calibration/calibration-peak-throughput.yaml'),
        ]);
        const baselineRoot = new URL('../llm_d_bench/deploy/providers/guide_overlays/precise-prefix-cache-routing/baseline/', import.meta.url);
        const baseline = valuesYaml(fs.readFileSync(new URL('service.yaml', baselineRoot), 'utf8'));
        const overlay = valuesYaml(fs.readFileSync(new URL('kustomization.yaml', baselineRoot), 'utf8'));
        baseline.metadata.name = `${overlay.namePrefix || ''}${baseline.metadata.name}`;
        for (const label of overlay.labels || []) baseline.metadata.labels = { ...baseline.metadata.labels, ...label.pairs };
        resources.push(asset('render.yaml', rendered), asset('baseline.yaml', yaml.dump(baseline)));
        /* calibrate.sh reads this Job template from its own directory. */
        calibration = [asset('calibrate.sh', script), asset('calibration-peak-throughput.yaml', jobTemplate)];
    }
    // Keep original layers for audit, and one merged effective layer for install.
    return {
        schemaVersion: 'guide-deployment-bundle.v1', guide, sourceCommit: source.commit,
        helm: { chart: 'oci://ghcr.io/llm-d/charts/llm-d-router-standalone', version: ROUTER_CHART_VERSION, releaseName: guide,
            values: [asset('router-base.yaml', baseText), asset('router-guide.yaml', guideText), asset('router-effective.yaml', yaml.dump(values, { lineWidth: -1 }))],
        },
        resources, ...(calibration ? { calibration } : {}),
    };
}
