import { isDefaultRuntimeImage, runtimeImageRepository } from '../../components/benchmark-results/acceleratorDisplay';

// The hardware profile owns the model-server repository and version, so a saved
// artifact carries the pinned tag while the wizard setup may hold a repo-only
// default. Compare managed model-server images by repository.
function sameRuntimeImage(artifactImage, setupImage) {
    if (artifactImage === setupImage) return true;
    return isDefaultRuntimeImage(artifactImage)
        && isDefaultRuntimeImage(setupImage)
        && runtimeImageRepository(artifactImage) === runtimeImageRepository(setupImage);
}

// Saved manifests are immutable: only reuse artifacts matching the task's setup.
export function matchesEvaluationSetup(artifact, setup) {
    const configuration = artifact?.deployable_configuration || {};
    const content = configuration.content || {};
    const runtime = content.runtime || {};
    const cluster = configuration.provenance?.cluster_ref || {};
    const modelSource = runtime.modelSource || 'auto-cache';
    const setupModelSource = setup.modelSource || 'auto-cache';
    const sameStorage = setupModelSource !== 'auto-cache'
        || runtime.storageVolumeId === setup.storageVolumeId;
    const sameModelPath = setupModelSource !== 'shared-path'
        || runtime.mountPath === setup.modelPath;
    const sameBuildSource = setup.imageMode !== 'build-from-source'
        || runtime.buildSourceUrl === setup.buildSourceUrl;

    return cluster.id === setup.cluster.id
        && content.model?.name === setup.model.trim()
        && String(runtime.modelServer || '').toLowerCase() === String(setup.modelServer || '').toLowerCase()
        && sameRuntimeImage(runtime.image, setup.image)
        && modelSource === setupModelSource
        && (runtime.imageMode || 'use-upstream-image') === setup.imageMode
        && sameStorage
        && sameModelPath
        && sameBuildSource;
}

export function retainCompatibleConfigurations(artifacts, selectedIds, setup) {
    const compatible = artifacts.filter((artifact) => matchesEvaluationSetup(artifact, setup));
    return selectedIds.filter((id) => compatible.some((artifact) => artifact.artifact_id === id));
}
