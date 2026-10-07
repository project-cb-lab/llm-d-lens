"""Deploy checksum-bound manifests published by Configuration."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from llm_d_bench.common.hashing import stable_hash
from llm_d_bench.deploy.providers.deployment_bundle import validate_deployment_bundle
from llm_d_bench.deploy.providers.guide_adapter import (
    GuideAdapter,
    GuideDefinition,
    GuideDeploymentArtifact,
    ValidationResult,
)
from llm_d_bench.deploy.providers.hardware_profile import accelerator_supported
from llm_d_bench.deploy.providers.target_node import configured_target_node, write_pinned_manifest
from llm_d_bench.monitoring.kv_trace.manifest import instrument_manifest
from llm_d_bench.storage.service import ensure_mount_in_namespace, get_ready_volume
from llm_d_bench.utils.artifact_store import register_artifacts
from llm_d_bench.utils.artifacts import DEPLOYMENT_MANIFEST_DIR, text_checksum, validate_configuration_manifest_content


class ConfigurationManifestAdapter:
    """Use Configuration's manifest when present, otherwise retain legacy behavior."""

    def __init__(
        self,
        delegate: GuideAdapter,
        command_runner,
        namespace_prefix: str,
        readiness_timeout_seconds: int,
        output_root: Path = DEPLOYMENT_MANIFEST_DIR,
    ) -> None:
        self._delegate = delegate
        self._runner = command_runner
        self._namespace_prefix = namespace_prefix
        self._readiness_timeout_seconds = readiness_timeout_seconds
        self._output_root = output_root.resolve()

    def discover(self) -> GuideDefinition:
        return self._delegate.discover()

    def register_restored_namespace(self, namespace: str) -> None:
        register = getattr(self._runner, "register_restored_namespace", None)
        if callable(register):
            register(namespace)
        delegate_register = getattr(self._delegate, "register_restored_namespace", None)
        if callable(delegate_register):
            delegate_register(namespace)

    def validate_inputs(
        self, definition: GuideDefinition, cluster_snapshot: dict[str, Any], overrides: dict[str, Any]
    ) -> ValidationResult:
        if not self._is_exact(overrides):
            return self._delegate.validate_inputs(definition, cluster_snapshot, overrides)
        try:
            validate_configuration_manifest_content(overrides, definition.guide_id)
            source = (overrides.get("officialGuide") or {}).get("source") or {}
            if not accelerator_supported(source.get("accelerator")):
                raise ValueError(
                    f"{definition.guide_id} does not support accelerator "
                    f"{source.get('accelerator')!r} in this Prism release"
                )
            allowed_servers = {"vllm", "vllm-rdma"} if definition.guide_id == "pd-disaggregation" else {"vllm"}
            if source.get("modelServer") not in allowed_servers:
                raise ValueError(
                    f"{definition.guide_id} does not support model server {source.get('modelServer')} "
                    "in this Prism release"
                )
        except ValueError as error:
            return ValidationResult(False, [str(error)])
        return ValidationResult(True)

    async def render(self, definition: GuideDefinition, overrides: dict[str, Any]) -> GuideDeploymentArtifact:
        if not self._is_exact(overrides):
            artifact = await self._delegate.render(definition, overrides)
        else:
            manifest, source_checksum, deployment, source = validate_configuration_manifest_content(
                overrides, definition.guide_id
            )
            deployment_contract = dict(deployment)
            bundle = (overrides.get("officialGuide") or {}).get("deploymentBundle")
            if bundle is not None:
                validate_deployment_bundle(bundle, definition.guide_id, str(source.get("commit") or ""))
                deployment_contract["deploymentBundle"] = bundle
            router_profile = overrides.get("routerProfile")
            if isinstance(router_profile, str) and router_profile:
                deployment_contract["routerProfile"] = router_profile
            namespace = str(overrides.get("_deployment_namespace") or "")
            self._validate_namespace(namespace)
            rendered = manifest.replace("__PRISM_NAMESPACE__", namespace)
            if "__PRISM_NAMESPACE__" in rendered:
                raise ValueError("deployment manifest contains an unresolved namespace binding")
            manifest_checksum = text_checksum(rendered)
            directory = self._output_root / manifest_checksum.removeprefix("sha256:")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "manifest.yaml"
            if path.is_file() and path.read_text(encoding="utf-8") != rendered:
                raise ValueError("checksum-addressed deployment manifest drift detected")
            if not path.is_file():
                path.write_text(rendered, encoding="utf-8")
            register_artifacts(
                directory,
                owner_type="deployment-manifest",
                owner_id=manifest_checksum.removeprefix("sha256:"),
                source_version=source.get("commit"),
                retention_class="configuration",
            )
            artifact = GuideDeploymentArtifact(
                guide_id=definition.guide_id,
                artifact_hash=stable_hash(
                    {
                        "source": source_checksum,
                        "manifest": manifest_checksum,
                        "namespace": namespace,
                        "deployment_contract": deployment_contract,
                    }
                ),
                manifest_ref=str(path),
                source_ref=str(source.get("commit")),
                guide_content_hash=source_checksum,
                manifest_checksum=manifest_checksum,
                deployment_contract=deployment_contract,
            )

        artifact = instrument_manifest(artifact, self._output_root)
        if artifact.manifest_ref and artifact.manifest_checksum:
            saved_path = Path(artifact.manifest_ref)
            if saved_path.is_file() and saved_path.resolve().is_relative_to(self._output_root.resolve()):
                register_artifacts(
                    saved_path.parent,
                    owner_type="deployment-manifest",
                    owner_id=artifact.manifest_checksum.removeprefix("sha256:"),
                    source_version=artifact.source_ref,
                    retention_class="configuration",
                )
        target_node = configured_target_node()
        if not target_node:
            return artifact
        if not artifact.manifest_ref:
            raise ValueError("target-node enforcement requires a rendered Kubernetes manifest")
        if Path(artifact.manifest_ref).is_dir():
            # Descriptor/Kustomize providers own directory overlays and must
            # apply node pinning while rendering their generated patch. The
            # generic pinning helper accepts a single rendered YAML file only.
            return artifact
        manifest_ref, manifest_checksum, pinned_workloads = write_pinned_manifest(
            artifact.manifest_ref, self._output_root, target_node
        )
        return replace(
            artifact,
            artifact_hash=stable_hash(
                {
                    "provider_artifact": artifact.artifact_hash,
                    "manifest": manifest_checksum,
                    "target_node": target_node,
                    "pinned_workloads": pinned_workloads,
                }
            ),
            manifest_ref=manifest_ref,
            manifest_checksum=manifest_checksum,
        )

    async def deploy(self, artifact: GuideDeploymentArtifact, execution_context: dict[str, Any]) -> dict[str, Any]:
        if not artifact.deployment_contract:
            return await self._delegate.deploy(artifact, execution_context)
        if Path(artifact.manifest_ref or "").is_dir():
            # Legacy Guide adapters may return a Kustomize overlay directory
            # rather than an immutable Configuration manifest file.
            return await self._delegate.deploy(artifact, execution_context)
        namespace = self._namespace(execution_context)
        status, stdout, stderr = await self._runner(["kubectl", "create", "namespace", namespace])
        if status != 0 and "alreadyexists" not in (stdout + stderr).lower():
            raise RuntimeError((stderr or stdout or "namespace creation failed").strip())
        manifest = Path(artifact.manifest_ref or "").read_text(encoding="utf-8")
        model_token = execution_context.get("model_token")
        if "name: llm-d-hf-token" in manifest and model_token:
            create_secret = getattr(self._runner, "create_model_secret_from_token", None)
            if not callable(create_secret):
                raise RuntimeError("deployment runner cannot provision the supplied model token")
            await create_secret(namespace, model_token)
        elif "name: llm-d-hf-token" in manifest:
            policy = execution_context.get("deployment_policy") or {}
            model_secret = policy.get("model_secret") or {"mode": "none"}
            if model_secret.get("mode") == "existing-secret":
                copy_secret = getattr(self._runner, "copy_model_secret", None)
                source_namespace = str(model_secret.get("sourceNamespace") or "")
                source_name = str(model_secret.get("sourceName") or "")
                if not callable(copy_secret) or not source_namespace or not source_name:
                    raise RuntimeError("existing model Secret source is not configured")
                await copy_secret(namespace, source_namespace, source_name)
            elif model_secret.get("mode") == "host":
                create_secret = getattr(self._runner, "create_model_secret", None)
                token_file = Path.home() / ".cache" / "huggingface" / "token"
                if not callable(create_secret) or not token_file.is_file():
                    raise RuntimeError(
                        "deployment requires a model token; select an existing Kubernetes Secret or "
                        "configure a host token"
                    )
                await create_secret(namespace, token_file)
        install_router = (
            getattr(self._delegate, "install_published_router", None)
            if callable(getattr(type(self._delegate), "install_published_router", None))
            else None
        )
        runtime = execution_context.get("runtime") or {}
        storage_volume_id = str(runtime.get("storageVolumeId") or "")
        cluster_id = str((execution_context.get("provenance") or {}).get("cluster_server_id") or "")
        if storage_volume_id:
            # Must run before any delegate-lifecycle early return below: the
            # rendered manifest (from Configuration) already embeds a
            # `persistentVolumeClaim` volume referencing the storage volume's
            # PVC name, but that PVC only exists in Storage's home namespace
            # (see storage.service.ensure_mount_in_namespace). Guides such as
            # pd-disaggregation delegate their whole `deploy()` to install
            # Guide-specific Router/EPP components before applying the
            # manifest, which would otherwise skip this and leave the Pod
            # referencing a PVC that doesn't exist in its own namespace.
            volume = await get_ready_volume(storage_volume_id, cluster_id=cluster_id or None)
            await ensure_mount_in_namespace(volume, namespace=namespace, cluster_id=cluster_id)
        if callable(install_router):
            await install_router(artifact, execution_context)
        elif self._uses_delegate_lifecycle(artifact):
            # Published manifests contain model-server resources, while these
            # providers also own Router or other Guide-specific components.
            return await self._delegate.deploy(artifact, execution_context)
        status, stdout, stderr = await self._runner(
            ["kubectl", "apply", "--namespace", namespace, "--filename", artifact.manifest_ref or ""]
        )
        if status != 0:
            raise RuntimeError((stderr or stdout or "manifest apply failed").strip())
        reproducibility = {
            key: value
            for key, value in execution_context.items()
            if key.startswith("router_effective_")
            or key.startswith("router_rendered_")
            or key
            in {
                "router_values_path",
                "router_chart",
                "router_version",
                "router_release_name",
                "calibration_script_path",
            }
        }
        return {
            "namespace": namespace,
            "artifact_hash": artifact.artifact_hash,
            "apply_output": stdout,
            **reproducibility,
        }

    async def readiness(self, execution: dict[str, Any]) -> ValidationResult:
        artifact = execution.get("_artifact")
        if not isinstance(artifact, GuideDeploymentArtifact) or not artifact.deployment_contract:
            return await self._delegate.readiness(execution)
        if self._uses_delegate_lifecycle(artifact):
            return await self._delegate.readiness(execution)
        namespace = self._namespace(execution)
        for name in artifact.deployment_contract["readinessDeployments"]:
            # A successful apply can return before the API server's subsequent
            # read path observes the Deployment.  Retry only this propagation
            # race; real rollout failures must still be reported immediately.
            for attempt in range(10):
                status, stdout, stderr = await self._runner(
                    [
                        "kubectl",
                        "rollout",
                        "status",
                        f"deployment/{name}",
                        "--namespace",
                        namespace,
                        f"--timeout={self._readiness_timeout_seconds}s",
                    ]
                )
                message = (stderr or stdout or f"deployment/{name} not ready").strip()
                if status == 0 or not self._is_transient_not_found(message) or attempt == 9:
                    break
                await asyncio.sleep(1)
            if status != 0:
                return ValidationResult(False, [message])
        endpoint = artifact.deployment_contract["endpoint"]
        execution["endpoint_url"] = (
            f"{endpoint['protocol']}://{endpoint['serviceName']}.{namespace}.svc.cluster.local:{endpoint['port']}"
        )
        return ValidationResult(True)

    @staticmethod
    def _is_transient_not_found(message: str) -> bool:
        """Recognize only the API propagation error that is safe to retry."""
        normalized = message.lower()
        return "deployments.apps" in normalized and "not found" in normalized

    async def diagnostics(self, execution: dict[str, Any]) -> dict[str, Any]:
        artifact = execution.get("_artifact")
        if not isinstance(artifact, GuideDeploymentArtifact) or not artifact.deployment_contract:
            return await self._delegate.diagnostics(execution)
        if self._uses_delegate_lifecycle(artifact):
            return await self._delegate.diagnostics(execution)
        namespace = self._namespace(execution)

        async def capture(arguments: list[str]) -> str:
            status, stdout, stderr = await self._runner(arguments)
            return (stdout if status == 0 else stderr).strip()

        return {
            "namespace": namespace,
            "pods": await capture(["kubectl", "get", "pods", "--namespace", namespace, "-o", "wide"]),
            "events": await capture(["kubectl", "get", "events", "--namespace", namespace, "--sort-by=.lastTimestamp"]),
            "resource_claims": await capture(
                ["kubectl", "get", "resourceclaims.resource.k8s.io", "--namespace", namespace, "-o", "wide"]
            ),
        }

    async def stop(self, execution, artifact):
        if not artifact.deployment_contract:
            return await self._delegate.stop(execution, artifact)
        if self._uses_delegate_lifecycle(artifact):
            return await self._delegate.stop(execution, artifact)
        namespace = self._namespace(execution)
        results = [
            await self._runner(["kubectl", "scale", f"deployment/{name}", "--namespace", namespace, "--replicas=0"])
            for name in artifact.deployment_contract["readinessDeployments"]
        ]
        return {
            "stopped": all(result[0] == 0 for result in results),
            "output": "\n".join(result[1] for result in results),
        }

    async def rollback(self, execution, artifact):
        return await self.stop(execution, artifact)

    async def cleanup(self, execution, artifact, *, force: bool = False):
        if not artifact.deployment_contract:
            return await self._delegate.cleanup(execution, artifact, force=force)
        if self._uses_delegate_lifecycle(artifact):
            return await self._delegate.cleanup(execution, artifact, force=force)
        namespace = self._namespace(execution)
        status, stdout, stderr = await self._runner(
            ["kubectl", "delete", "namespace", namespace, "--ignore-not-found=true"]
        )
        return {"cleaned_up": status == 0, "output": stdout, "error": stderr or None}

    @staticmethod
    def _is_exact(overrides: dict[str, Any]) -> bool:
        return isinstance(overrides.get("officialGuide"), dict)

    def _uses_delegate_lifecycle(self, artifact: GuideDeploymentArtifact) -> bool:
        return artifact.guide_id == "pd-disaggregation" or bool(
            getattr(type(self._delegate), "published_manifest_lifecycle", False)
        )

    def _namespace(self, context: dict[str, Any]) -> str:
        namespace = str(context.get("namespace") or "")
        self._validate_namespace(namespace)
        return namespace

    def _validate_namespace(self, namespace: str) -> None:
        if (
            not namespace.startswith(self._namespace_prefix)
            or namespace == self._namespace_prefix
            or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace)
        ):
            raise ValueError("deployment namespace is outside the configured allowlist")
