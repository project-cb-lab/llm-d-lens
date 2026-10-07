// Release-time consistency check for the pinned llm-d stack profile.
//
// Verifies that the versions in llm_d_bench/versions/llm_d_stack.yaml are
// internally consistent and actually exist, so an incompatible combination
// cannot ship. Run in CI before a release, and locally before changing the
// profile:
//
//   node tools/check-llm-d-stack.mjs           # static + network checks
//   node tools/check-llm-d-stack.mjs --offline # static checks only
//
// A non-zero exit means the profile must be fixed (or the referenced version
// published) before release.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import yaml from 'js-yaml';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const PROFILE_PATH = path.join(ROOT, 'llm_d_bench', 'versions', 'llm_d_stack.yaml');
// Model-server images are owned by the hardware profiles (repository + version),
// not derived from `llm_d`; scan the profile registry directory rather than a
// hardcoded vendor file list.
const HARDWARE_PROFILES_DIR = path.join(ROOT, 'llm_d_bench', 'hardware', 'profiles');

const GITHUB_TOKEN = process.env.GITHUB_TOKEN || process.env.GH_TOKEN || '';
const REQUIRED_KEYS = [
  'llm_d',
  'llm_d_router',
  'llm_d_benchmark',
  'llm_d_inference_payload_processor',
  'k8s_gateway_api',
  'k8s_gateway_api_inference_extension',
];
const REQUIRED_GATEWAY_PROVIDERS = ['istio', 'envoy_gateway', 'envoy_ai_gateway', 'agentgateway'];

export function loadProfile(filePath = PROFILE_PATH) {
  return yaml.load(fs.readFileSync(filePath, 'utf8'));
}

//: Split an image reference into repository and tag (digest-pinned images fall
//: back to the digest tag).
export function splitImageReference(image) {
  const withoutDigest = String(image).split('@')[0];
  const lastColon = withoutDigest.lastIndexOf(':');
  const lastSlash = withoutDigest.lastIndexOf('/');
  if (lastColon > lastSlash) {
    return { repository: withoutDigest.slice(0, lastColon), tag: withoutDigest.slice(lastColon + 1) };
  }
  return { repository: withoutDigest, tag: 'latest' };
}

//: Every model-server image the hardware profiles pin, read from the profile
//: directory so a vendor/version change is data, not a code edit here.
export function loadModelServerImages() {
  const images = [];
  for (const entry of fs.readdirSync(HARDWARE_PROFILES_DIR)) {
    if (!entry.endsWith('.json')) continue;
    const profile = JSON.parse(fs.readFileSync(path.join(HARDWARE_PROFILES_DIR, entry), 'utf8'));
    const image = profile?.deployment?.runtime_image;
    if (typeof image === 'string' && image) images.push(image);
  }
  return images;
}

export function validateStatic(profile) {
  const problems = [];
  for (const key of REQUIRED_KEYS) {
    if (typeof profile?.[key] !== 'string' || !profile[key].trim()) {
      problems.push(`missing or empty "${key}"`);
    }
  }
  for (const provider of REQUIRED_GATEWAY_PROVIDERS) {
    if (typeof profile?.gateway_providers?.[provider] !== 'string' || !profile.gateway_providers[provider].trim()) {
      problems.push(`missing or empty "gateway_providers.${provider}"`);
    }
  }
  return problems;
}

// `export ROUTER_CHART_VERSION=${ROUTER_CHART_VERSION:-v0.10.0}`
export function parseEnvSh(text) {
  const values = {};
  for (const key of ['ROUTER_CHART_VERSION', 'GATEWAY_API_VERSION', 'GAIE_VERSION']) {
    const match = text.match(new RegExp(`export ${key}=\\$\\{[^:}]+:-([^}]+)\\}`));
    if (match) values[key] = match[1].trim();
  }
  return values;
}

async function safeFetch(url, init) {
  try {
    return await fetch(url, init);
  } catch {
    return null;
  }
}

async function githubTagExists(repo, tag) {
  const response = await safeFetch(`https://api.github.com/repos/${repo}/git/ref/tags/${tag}`, {
    headers: GITHUB_TOKEN ? { Authorization: `Bearer ${GITHUB_TOKEN}` } : {},
  });
  return Boolean(response?.ok);
}

async function ghcrTagExists(repository, tag) {
  const tokenResponse = await safeFetch(`https://ghcr.io/token?scope=repository:${repository}:pull&service=ghcr.io`);
  if (!tokenResponse?.ok) return false;
  const { token } = await tokenResponse.json().catch(() => ({}));
  const response = await safeFetch(`https://ghcr.io/v2/${repository}/tags/list`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response?.ok) return false;
  const { tags = [] } = await response.json().catch(() => ({}));
  return tags.includes(tag);
}

async function fetchText(url) {
  const response = await safeFetch(url);
  return response?.ok ? response.text() : null;
}

export async function runChecks(profile, { offline = false, log = console.log } = {}) {
  const failures = [];
  const warnings = [];
  const record = (ok, message) => {
    log(`${ok ? 'ok  ' : 'FAIL'} ${message}`);
    if (!ok) failures.push(message);
  };
  const note = (condition, message) => {
    if (!condition) {
      warnings.push(message);
      log(`warn ${message}`);
    }
  };

  for (const problem of validateStatic(profile)) record(false, `profile: ${problem}`);
  if (failures.length) return { failures, warnings };
  record(true, 'profile has all required versions');

  if (offline) {
    log('offline: skipping network checks');
    return { failures, warnings };
  }

  const {
    llm_d: llmD,
    llm_d_router: router,
    llm_d_benchmark: benchmark,
    llm_d_inference_payload_processor: ipp,
    k8s_gateway_api: gatewayApi,
    k8s_gateway_api_inference_extension: gie,
  } = profile;

  for (const [repo, tag] of [
    ['llm-d/llm-d', llmD],
    ['llm-d/llm-d-router', router],
    ['llm-d/llm-d-benchmark', benchmark],
  ]) {
    record(await githubTagExists(repo, tag), `${repo}@${tag} exists`);
  }

  for (const [repository, tag] of [
    ['llm-d/charts/llm-d-router-standalone', router],
    ['llm-d/llm-d-router-endpoint-picker', router],
    ['llm-d/llm-d-router-disagg-sidecar', router],
    ['llm-d/charts/payload-processor', ipp],
  ]) {
    record(await ghcrTagExists(repository, tag), `ghcr.io/${repository}:${tag} exists`);
  }

  for (const image of loadModelServerImages()) {
    const { repository, tag } = splitImageReference(image);
    if (!repository.startsWith('ghcr.io/')) {
      note(false, `model-server image ${image} is not on ghcr (not verified)`);
      continue;
    }
    const repo = repository.slice('ghcr.io/'.length);
    record(await ghcrTagExists(repo, tag), `ghcr.io/${repo}:${tag} exists`);
  }

  const envSh = await fetchText(`https://raw.githubusercontent.com/llm-d/llm-d/${llmD}/guides/env.sh`);
  if (envSh === null) {
    record(false, `llm-d ${llmD} guides/env.sh is reachable`);
  } else {
    const pinned = parseEnvSh(envSh);
    record(pinned.ROUTER_CHART_VERSION === router, `llm-d ${llmD} pins router ${pinned.ROUTER_CHART_VERSION} = profile ${router}`);
    record(pinned.GATEWAY_API_VERSION === gatewayApi, `llm-d ${llmD} pins Gateway API ${pinned.GATEWAY_API_VERSION} = profile ${gatewayApi}`);
    record(pinned.GAIE_VERSION === gie, `llm-d ${llmD} pins Gateway API Inference Extension ${pinned.GAIE_VERSION} = profile ${gie}`);
  }

  const defaults = await fetchText(
    `https://raw.githubusercontent.com/llm-d/llm-d-benchmark/${benchmark}/config/templates/values/defaults.yaml`,
  );
  if (defaults === null) {
    record(false, `llm-d-benchmark ${benchmark} defaults.yaml is reachable`);
  } else {
    const chartVersions = yaml.load(defaults)?.chartVersions || {};
    record(
      Object.prototype.hasOwnProperty.call(chartVersions, 'llmDRouter'),
      `llm-d-benchmark ${benchmark} exposes chartVersions.llmDRouter (Lens overrides it)`,
    );
    note(
      chartVersions.llmDRouter === router,
      `llm-d-benchmark ${benchmark} defaults llmDRouter=${chartVersions.llmDRouter} (profile ${router}; Lens overrides it)`,
    );
  }

  return { failures, warnings };
}

async function main() {
  const offline = process.argv.includes('--offline');
  const profile = loadProfile();
  let failures;
  try {
    ({ failures } = await runChecks(profile, { offline }));
  } catch (error) {
    console.error(`llm-d stack profile check could not run: ${error}`);
    process.exit(1);
  }
  if (failures.length) {
    console.error(`\nllm-d stack profile is inconsistent (${failures.length} problem(s)); see above.`);
    process.exit(1);
  }
  console.log('\nllm-d stack profile is consistent.');
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
