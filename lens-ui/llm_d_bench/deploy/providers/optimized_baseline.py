"""Controlled lifecycle adapter for a pinned local optimized-baseline Guide."""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from llm_d_bench.common.hashing import stable_hash
from llm_d_bench.configuration.service import CONFIGURATION_OUTPUT_DIR
from llm_d_bench.deploy.data_plane import router_data_plane_args, router_data_plane_effective_values
from llm_d_bench.deploy.providers.deployment_bundle import (
    install_deployment_bundle,
    subprocess_bundle_runner,
    validate_deployment_bundle,
)
from llm_d_bench.deploy.providers.gpu_selection import gpu_device_selectors
from llm_d_bench.deploy.providers.guide_adapter import (
    GuideAdapter,
    GuideDefinition,
    GuideDeploymentArtifact,
    ValidationResult,
)
from llm_d_bench.deploy.providers.model_cache_environment import model_cache_environment
from llm_d_bench.deploy.providers.storage_mount import resolve_mount
from llm_d_bench.deploy.providers.target_node import configured_target_node

CommandRunner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


@dataclass(frozen=True)
class OptimizedBaselineGuidePolicy:
    """Immutable paths and release identities approved for one local Guide."""

    namespace_prefix: str
    guide_root: Path
    overlay_path: Path
    router_base_values_path: Path
    router_values_path: Path
    neutral_router_values_path: Path
    gateway_mode_values_path: Path
    router_chart: str
    router_chart_version: str
    router_release_name: str
    model_deployment_name: str
    endpoint_service_name: str
    endpoint_service_port: int
    baseline_service_name: str
    baseline_service_port: int
    model_token_file: Path | None = None
    rendered_overlay_root: Path = Path()
    proxy_environment: dict[str, str] = field(default_factory=dict)
    retain_namespace_on_failure: bool = False
    readiness_timeout_seconds: int = 900
    docker_path: Path | None = None


class OptimizedBaselineGuideAdapter(GuideAdapter):
    """Deploy the upstream local XPU overlay without reimplementing the Guide."""

    published_manifest_lifecycle = True

    def __init__(
        self,
        definition: GuideDefinition,
        command_runner: CommandRunner,
        *,
        policy: OptimizedBaselineGuidePolicy,
    ) -> None:
        self._definition = definition
        self._command_runner = command_runner
        self._policy = policy
        kubectl_runner = getattr(command_runner, "_kubectl", command_runner)
        self._bundle_command_runner = subprocess_bundle_runner(
            Path(getattr(command_runner, "_helm_path", "helm")),
            Path(getattr(kubectl_runner, "_kubectl_path", "kubectl")),
            getattr(kubectl_runner, "_environment", None),
        )

    def discover(self) -> GuideDefinition:
        return self._definition

    def _gateway_mode_args(self, execution_context: Mapping[str, Any] | None) -> list[str]:
        """Extra Helm args that switch the chart to the shared Gateway.

        Delegates to the shared data-plane helper; evaluation-owned deployments
        keep their own proxy and get nothing here.
        """
        return router_data_plane_args(execution_context)

    def register_restored_namespace(self, namespace: str) -> None:
        register = getattr(self._command_runner, "register_restored_namespace", None)
        if callable(register):
            register(namespace)

    def validate_inputs(
        self,
        definition: GuideDefinition,
        cluster_snapshot: dict[str, Any],
        overrides: dict[str, Any],
    ) -> ValidationResult:
        if definition.guide_id != self._definition.guide_id:
            return ValidationResult(accepted=False, reasons=["unsupported_guide_mapping"])
        try:
            deployment, _model_name, _runtime = self._deployment_parameters(overrides)
        except (TypeError, ValueError) as error:
            return ValidationResult(accepted=False, reasons=[f"invalid deployment override: {error}"])
        if any(value is not None and value <= 0 for value in deployment.values()):
            return ValidationResult(accepted=False, reasons=["deployment overrides must be positive integers"])
        if deployment["accelerator_count"] != deployment["tensor_parallel_size"]:
            return ValidationResult(
                accepted=False,
                reasons=[
                    "accelerator_count must equal tensor_parallel_size for the registered single-node XPU overlay"
                ],
            )
        # Capacity is advisory: Kubernetes may keep the requested pods Pending.
        required_paths = (
            self._policy.guide_root,
            self._policy.overlay_path,
            self._policy.router_values_path,
        )
        if not all(path.exists() and path.is_relative_to(self._policy.guide_root) for path in required_paths):
            return ValidationResult(accepted=False, reasons=["registered_guide_artifact_missing"])
        return ValidationResult(accepted=True)

    async def render(self, definition: GuideDefinition, overrides: dict[str, Any]) -> GuideDeploymentArtifact:
        if definition.guide_id != self._definition.guide_id:
            raise ValueError("adapter cannot render a different guide")
        deployment, model_id, runtime = self._deployment_parameters(overrides)
        runtime["resolved_mount"] = await resolve_mount(
            overrides.get("runtime") or {},
            cluster_id=overrides.get("_cluster_id"),
            namespace=overrides.get("_deployment_namespace"),
        )
        if runtime["image_mode"] == "build-from-source":
            await self._build_image(runtime["build_source_url"], runtime["image"])
        rendered_overlay = self._render_overlay(deployment, model_id, runtime)
        contents = {
            "overlay": _tree_checksum(self._policy.overlay_path),
            "router_values": self._policy.router_values_path.read_text(encoding="utf-8"),
        }
        manifest_checksum = stable_hash(contents)
        return GuideDeploymentArtifact(
            guide_id=definition.guide_id,
            artifact_hash=stable_hash(
                {
                    "guide": definition.content_hash,
                    "source_ref": definition.source_ref,
                    "manifest_checksum": manifest_checksum,
                    "overrides": overrides,
                    "rendered_overlay": str(rendered_overlay),
                }
            ),
            manifest_ref=str(rendered_overlay),
            values_checksum=stable_hash({"overrides": overrides}),
            source_ref=definition.source_ref,
            guide_content_hash=definition.content_hash,
            manifest_checksum=manifest_checksum,
            deployment_contract={
                "routerProfile": overrides.get("routerProfile", "optimized-baseline"),
                # llm-d router data plane: non-evaluation deployments run no proxy
                # and are reached through the cluster's shared Gateway;
                # evaluation-owned deployments keep their own proxy (see deploy()).
                "data_plane_kind": "llm-d-router",
            },
        )

    async def install_published_router(
        self, artifact: GuideDeploymentArtifact, execution_context: dict[str, Any]
    ) -> None:
        """Install the routed data plane before an exact published manifest is applied."""
        namespace = self._namespace(execution_context)
        profile = artifact.deployment_contract.get("routerProfile", "optimized-baseline")
        bundle = artifact.deployment_contract.get("deploymentBundle")
        if bundle is not None:
            validate_deployment_bundle(bundle, artifact.guide_id, str(artifact.source_ref or ""))
            manifest_ref = Path(artifact.manifest_ref or "").resolve()
            output_root = (manifest_ref if manifest_ref.is_dir() else manifest_ref.parent) / "deployment-bundle"
            saved_effective = bundle["helm"]["values"][-1]["content"]
            base_values = (
                saved_effective
                if profile == "optimized-baseline"
                else self._profiled_router_values_content(saved_effective, profile)
            )
            effective_values = router_data_plane_effective_values(base_values, execution_context)
            result = await install_deployment_bundle(
                bundle,
                guide=artifact.guide_id,
                source_commit=str(artifact.source_ref or ""),
                namespace=namespace,
                command_runner=self._bundle_command_runner,
                output_root=output_root,
                effective_values_content=effective_values,
                effective_values_name=f"router-effective-{profile}.yaml",
            )
            execution_context.update(result)
            return
        values = self._router_values_for_profile(profile, Path(artifact.manifest_ref or "").resolve())
        command = [
            "helm",
            "upgrade",
            "--install",
            self._policy.router_release_name,
            self._policy.router_chart,
            "--namespace",
            namespace,
            "--version",
            self._policy.router_chart_version,
            "--values",
            str(self._policy.router_base_values_path),
            "--values",
            str(values),
            *self._gateway_mode_args(execution_context),
        ]
        status, stdout, stderr = await self._command_runner(command)
        if status != 0:
            raise RuntimeError((stderr or stdout or "Router deployment failed").strip())

    async def deploy(self, artifact: GuideDeploymentArtifact, execution_context: dict[str, Any]) -> dict[str, Any]:
        self._validate_artifact(artifact)
        namespace = self._namespace(execution_context)
        rendered_overlay = Path(artifact.manifest_ref or "").resolve()
        status, stdout, stderr = await self._command_runner(["kubectl", "create", "namespace", namespace])
        if status != 0 and "alreadyexists" not in (stdout + stderr).lower():
            raise RuntimeError((stderr or stdout or "registered guide deployment failed").strip())
        deployment_policy = execution_context.get("deployment_policy") or {}
        model_secret = deployment_policy.get("model_secret") or {}
        model_token = execution_context.get("model_token")
        if model_token:
            create_secret = getattr(self._command_runner, "create_model_secret_from_token", None)
            if not callable(create_secret):
                raise RuntimeError("registered guide runner cannot provision the supplied model token")
            await create_secret(namespace, model_token)
        elif model_secret.get("mode") == "existing-secret":
            copy_secret = getattr(self._command_runner, "copy_model_secret", None)
            if not callable(copy_secret):
                raise RuntimeError("registered guide runner cannot copy the selected model secret")
            await copy_secret(
                namespace,
                str(model_secret.get("sourceNamespace") or ""),
                str(model_secret.get("sourceName") or ""),
            )
        elif (
            model_secret.get("mode") == "host"
            and self._policy.model_token_file
            and not self._deployment_parameters_from_artifact(artifact).get("mount_path")
        ):
            create_secret = getattr(self._command_runner, "create_model_secret", None)
            if not callable(create_secret):
                raise RuntimeError("registered guide runner cannot provision the required model secret")
            await create_secret(namespace, self._policy.model_token_file)
        plan = [
            [
                "helm",
                "upgrade",
                "--install",
                self._policy.router_release_name,
                self._policy.router_chart,
                "--namespace",
                namespace,
                "--version",
                self._policy.router_chart_version,
                "--values",
                str(self._policy.router_base_values_path),
                "--values",
                str(
                    self._router_values_for_profile(
                        artifact.deployment_contract.get("routerProfile", "optimized-baseline"),
                        rendered_overlay,
                    )
                ),
                *self._gateway_mode_args(execution_context),
            ],
            ["kubectl", "apply", "--namespace", namespace, "--kustomize", str(rendered_overlay)],
        ]
        for command in plan:
            status, stdout, stderr = await self._command_runner(command)
            if status != 0:
                raise RuntimeError((stderr or stdout or "registered guide deployment failed").strip())
        execution = {"namespace": namespace, "artifact_hash": artifact.artifact_hash}
        take_evidence_refs = getattr(self._command_runner, "take_evidence_refs", None)
        if callable(take_evidence_refs):
            execution["evidence_refs"] = take_evidence_refs()
        return execution

    def _router_values_for_profile(self, profile: str, rendered_overlay: Path) -> Path:
        """Resolve full/neutral/ablation EPP policy values without mutating the upstream Guide."""
        if profile in {"router-neutral", "router-round-robin"}:
            return self._policy.neutral_router_values_path
        if profile == "optimized-baseline":
            return self._policy.router_values_path
        if profile not in {"load-only", "affinity-only"}:
            raise ValueError(f"unsupported optimized-baseline router profile: {profile}")
        if not rendered_overlay.is_dir() or not rendered_overlay.is_relative_to(
            self._policy.rendered_overlay_root.resolve()
        ):
            raise ValueError("router ablation values must be generated inside the rendered deployment overlay")
        content = self._profiled_router_values_content(
            self._policy.router_values_path.read_text(encoding="utf-8"),
            profile,
        )
        target = rendered_overlay / f"router-{profile}.values.yaml"
        target.write_text(content, encoding="utf-8")
        return target

    @staticmethod
    def _profiled_router_values_content(content: str, profile: str) -> str:
        values = yaml.safe_load(content)
        if not isinstance(values, dict):
            raise ValueError("optimized-baseline Router values must be a YAML mapping")
        epp = ((values or {}).get("router") or {}).get("epp") or {}
        if profile in {"router-neutral", "router-round-robin"}:
            generated_name = "router-neutral-plugins.yaml"
            epp["pluginsConfigFile"] = generated_name
            epp["pluginsCustomConfig"] = {
                generated_name: yaml.safe_dump(
                    {
                        "apiVersion": "llm-d.ai/v1alpha1",
                        "kind": "EndpointPickerConfig",
                        "plugins": [{"type": "random-picker"}],
                        "schedulingProfiles": [
                            {
                                "name": "default",
                                "plugins": [{"pluginRef": "random-picker"}],
                            }
                        ],
                    },
                    sort_keys=False,
                )
            }
            return yaml.safe_dump(values, sort_keys=False)
        if profile not in {"load-only", "affinity-only"}:
            raise ValueError(f"unsupported optimized-baseline router profile: {profile}")
        custom = epp.get("pluginsCustomConfig") or {}
        config_name = epp.get("pluginsConfigFile")
        if not config_name or not isinstance(custom.get(config_name), str):
            raise ValueError("optimized-baseline router values do not contain the configured EPP plugin document")
        plugin_document = yaml.safe_load(custom[config_name])
        plugins = plugin_document.get("plugins") or []
        profiles = plugin_document.get("schedulingProfiles") or []
        keep_types = (
            {"inflight-load-producer", "token-load-scorer"}
            if profile == "load-only"
            else {"approx-prefix-cache-producer", "inflight-load-producer", "prefix-cache-affinity-filter"}
        )
        plugin_document["plugins"] = [item for item in plugins if item.get("type") in keep_types]
        kept_names = {item.get("name") or item.get("type") for item in plugin_document["plugins"]}
        for scheduling_profile in profiles:
            scheduling_profile["plugins"] = [
                item for item in scheduling_profile.get("plugins") or [] if item.get("pluginRef") in kept_names
            ]
        generated_name = f"optimized-baseline-{profile}-plugins.yaml"
        epp["pluginsConfigFile"] = generated_name
        epp["pluginsCustomConfig"] = {generated_name: yaml.safe_dump(plugin_document, sort_keys=False)}
        return yaml.safe_dump(values, sort_keys=False)

    @staticmethod
    def _deployment_parameters_from_artifact(artifact: GuideDeploymentArtifact) -> dict[str, Any]:
        manifest_ref = Path(artifact.manifest_ref or "")
        if not manifest_ref.is_dir():
            return {}
        patch = manifest_ref / "patch-vllm.yaml"
        if not patch.is_file():
            return {}
        try:
            return {"mount_path": "/model-cache" if "/model-cache" in patch.read_text(encoding="utf-8") else ""}
        except OSError:
            return {}

    def _register_published_deployments(self, namespace: str, contract: dict[str, Any]) -> None:
        register = getattr(self._command_runner, "register_published_deployments", None)
        if callable(register) and contract.get("readinessDeployments"):
            register(namespace, contract["readinessDeployments"])

    async def readiness(self, execution: dict[str, Any]) -> ValidationResult:
        namespace = str(execution["namespace"])
        timeout = f"--timeout={self._policy.readiness_timeout_seconds}s"
        artifact = execution.get("_artifact")
        contract = artifact.deployment_contract if isinstance(artifact, GuideDeploymentArtifact) else {}
        self._register_published_deployments(namespace, contract)
        for name in contract.get("readinessDeployments", [self._policy.model_deployment_name]):
            # Kustomize apply and rollout status use separate API requests.  A
            # newly created Deployment may briefly be invisible to the latter,
            # so absorb that narrow race without hiding genuine rollout errors.
            for attempt in range(10):
                status, stdout, stderr = await self._command_runner(
                    [
                        "kubectl",
                        "rollout",
                        "status",
                        f"deployment/{name}",
                        "--namespace",
                        namespace,
                        timeout,
                    ]
                )
                message = (stderr or stdout or "deployment_not_ready").strip()
                normalized = message.lower()
                transient_not_found = "deployments.apps" in normalized and "not found" in normalized
                if status == 0 or not transient_not_found or attempt == 9:
                    break
                await asyncio.sleep(1)
            if status != 0:
                return ValidationResult(accepted=False, reasons=[message])
        endpoint_url = (
            f"http://{self._policy.endpoint_service_name}.{namespace}.svc.cluster.local:"
            f"{self._policy.endpoint_service_port}"
        )
        baseline = contract.get("endpoint", {})
        baseline_endpoint_url = (
            f"{baseline.get('protocol', 'http')}://"
            f"{baseline.get('serviceName', self._policy.baseline_service_name)}.{namespace}.svc:"
            f"{baseline.get('port', self._policy.baseline_service_port)}"
        )
        smoke_test = getattr(self._command_runner, "endpoint_smoke_test", None)
        if not callable(smoke_test):
            return ValidationResult(accepted=False, reasons=["registered guide runner cannot smoke test the endpoint"])
        # Gateway Mode disables the chart's own proxy, so the EPP Service port has no
        # backend; validate the model server directly instead.
        smoke_result = await smoke_test(namespace, baseline_endpoint_url)
        if smoke_result is not None:
            return ValidationResult(accepted=False, reasons=[str(smoke_result)])
        execution["endpoint_url"] = endpoint_url
        execution["baseline_endpoint_url"] = baseline_endpoint_url
        execution["endpoint_classification"] = "routed_guide_endpoint"
        return ValidationResult(accepted=True)

    async def diagnostics(self, execution: dict[str, Any]) -> dict[str, Any]:
        namespace = str(execution["namespace"])

        async def capture(command: list[str]) -> str:
            status, stdout, stderr = await self._command_runner(command)
            return (stdout if status == 0 else stderr).strip()

        pods = await capture(["kubectl", "get", "pods", "--namespace", namespace, "-o", "wide"])
        events = await capture(["kubectl", "get", "events", "--namespace", namespace, "--sort-by=.lastTimestamp"])
        logs = await capture(
            [
                "kubectl",
                "logs",
                "--namespace",
                namespace,
                "-l",
                "llm-d.ai/role=decode",
                "-c",
                "modelserver",
                "--tail=120",
            ]
        )
        return {
            "namespace": namespace,
            "pods": pods,
            "events": events,
            "modelserver_logs": logs,
            **self._router_reproducibility(execution),
            "captured_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _router_reproducibility(execution: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in execution.items()
            if key.startswith("router_effective_") or key.startswith("router_rendered_")
        }

    async def stop(self, execution: dict[str, Any], artifact: GuideDeploymentArtifact) -> dict[str, Any]:
        namespace = str(execution["namespace"])
        self._register_published_deployments(namespace, artifact.deployment_contract)
        results = []
        for name in artifact.deployment_contract.get("readinessDeployments", [self._policy.model_deployment_name]):
            results.append(
                await self._command_runner(
                    [
                        "kubectl",
                        "scale",
                        f"deployment/{name}",
                        "--namespace",
                        namespace,
                        "--replicas=0",
                    ]
                )
            )
        return {
            "stopped": all(status == 0 for status, _, _ in results),
            "output": "\n".join(stdout for _, stdout, _ in results),
            "error": "\n".join(stderr for _, _, stderr in results if stderr) or None,
        }

    async def rollback(self, execution: dict[str, Any], artifact: GuideDeploymentArtifact) -> dict[str, Any]:
        namespace = str(execution["namespace"])
        status, stdout, stderr = await self._command_runner(
            ["helm", "uninstall", self._policy.router_release_name, "--namespace", namespace]
        )
        return {"rolled_back": status == 0, "output": stdout, "error": stderr or None}

    async def cleanup(
        self, execution: dict[str, Any], artifact: GuideDeploymentArtifact, *, force: bool = False
    ) -> dict[str, Any]:
        namespace = str(execution["namespace"])
        if self._policy.retain_namespace_on_failure and not force:
            return {"cleaned_up": False, "retained": True, "output": "", "error": None}
        status, stdout, stderr = await self._command_runner(
            ["kubectl", "delete", "namespace", namespace, "--ignore-not-found=true"]
        )
        return {"cleaned_up": status == 0, "output": stdout, "error": stderr or None}

    def remove_rendered_overlay(self, artifact: GuideDeploymentArtifact) -> bool:
        """Remove a deployment-owned overlay without touching the upstream Guide."""
        rendered_overlay = Path(artifact.manifest_ref or "").resolve()
        root = self._policy.rendered_overlay_root.resolve()
        if (
            not rendered_overlay.is_dir()
            or not rendered_overlay.is_relative_to(root)
            or not rendered_overlay.name.startswith("llm-d-bench-")
        ):
            return False
        shutil.rmtree(rendered_overlay)
        return True

    def _validate_artifact(self, artifact: GuideDeploymentArtifact) -> None:
        if artifact.guide_id != self._definition.guide_id:
            raise ValueError("artifact guide identity does not match registered adapter")
        source_matches = artifact.source_ref == self._definition.source_ref
        content_matches = artifact.guide_content_hash == self._definition.content_hash
        if not source_matches or not content_matches:
            raise ValueError("guide source drift detected before deployment")
        rendered_overlay = Path(artifact.manifest_ref or "").resolve()
        if not rendered_overlay.is_dir() or not rendered_overlay.is_relative_to(
            self._policy.rendered_overlay_root.resolve()
        ):
            raise ValueError("artifact manifest reference is outside the rendered overlay allowlist")
        current_checksum = stable_hash(
            {
                "overlay": _tree_checksum(self._policy.overlay_path),
                "router_values": self._policy.router_values_path.read_text(encoding="utf-8"),
            }
        )
        if artifact.manifest_checksum != current_checksum:
            raise ValueError("artifact manifest content drift detected before deployment")

    def _render_overlay(self, deployment: dict[str, int | None], model_id: str, runtime: dict[str, Any]) -> Path:
        image = runtime["image"]
        resolved_mount = runtime.get("resolved_mount")
        # The registered storage volume's read-only flag can flip the effective
        # source (see storage_mount.resolve_mount), so derive it from the resolved
        # mount when present rather than the requested runtime.modelSource.
        active_model_source = resolved_mount["model_source"] if resolved_mount else runtime["model_source"]
        cache_mounted = resolved_mount is not None or bool(runtime["mount_host_path"])
        served_model = (
            resolved_mount["mount_path"]
            if resolved_mount and active_model_source == "shared-path"
            else runtime["mounted_model_path"] or model_id
        )
        overlay_hash = stable_hash(
            {
                "guide": self._definition.content_hash,
                "deployment": deployment,
                "model_id": model_id,
                "runtime": runtime,
            }
        )
        rendered_overlay = self._policy.rendered_overlay_root / f"llm-d-bench-{overlay_hash}"
        if rendered_overlay.exists():
            shutil.rmtree(rendered_overlay)
        rendered_overlay.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self._policy.overlay_path, rendered_overlay)
        patch_path = rendered_overlay / "patch-vllm.yaml"
        claim_path = rendered_overlay / "resource-claim-template.yaml"
        source_document = runtime.get("configuration_source_document")
        if source_document is not None:
            (rendered_overlay / "configuration-source.yaml").write_text(
                yaml.safe_dump(source_document, sort_keys=False), encoding="utf-8"
            )
        patch = patch_path.read_text(encoding="utf-8")
        patch = re.sub(r"(?m)^(\s*replicas:)\s*\d+", rf"\g<1> {deployment['replicas']}", patch, count=1)
        model_argument = re.search(r'(?m)^(?P<indent>\s*)-\s+["\'](?P<model>[^"\']+)["\']$', patch)
        if model_argument is None:
            raise ValueError("registered vLLM patch is missing the positional model argument")
        indent = model_argument.group("indent")
        patch = f"{patch[: model_argument.start('model')]}{served_model}{patch[model_argument.end('model') :]}"
        model_argument = re.search(r'(?m)^(?P<indent>\s*)-\s+["\'](?P<model>[^"\']+)["\']$', patch)
        if model_argument is None:
            raise ValueError("rendered vLLM patch is missing the positional model argument")
        optional_arguments = [f'{indent}- "--tensor-parallel-size={deployment["tensor_parallel_size"]}"']
        if deployment["max_num_seqs"] is not None:
            optional_arguments.append(f'{indent}- "--max-num-seqs={deployment["max_num_seqs"]}"')
        if deployment["max_model_len"] is not None:
            optional_arguments.append(f'{indent}- "--max-model-len={deployment["max_model_len"]}"')
        patch = patch[: model_argument.end()] + "\n" + "\n".join(optional_arguments) + patch[model_argument.end() :]
        if resolved_mount is not None:
            patch = self._inject_model_cache_volume(
                patch, resolved_mount["volume_source"], read_only=resolved_mount["read_only"]
            )
        elif runtime["mount_host_path"]:
            patch = self._inject_model_cache_mount(
                patch, runtime["mount_host_path"], read_only=runtime["model_source"] == "shared-path"
            )
        cache_environment = model_cache_environment(active_model_source, cache_mounted=cache_mounted)
        patch = self._inject_model_environment(
            patch,
            {**(runtime.get("environment") or {}), **cache_environment},
        )
        target_node = configured_target_node()
        if target_node:
            patch_document = yaml.safe_load(patch)
            pod_spec = patch_document["spec"]["template"]["spec"]
            pod_spec.setdefault("nodeSelector", {})["kubernetes.io/hostname"] = target_node
            patch = yaml.safe_dump(patch_document, sort_keys=False)
        patch_path.write_text(patch, encoding="utf-8")
        image_path = rendered_overlay / "kustomization.yaml"
        image_config = image_path.read_text(encoding="utf-8")
        image_name, image_tag = _image_parts(image)
        # Remap whichever image reference the guide base uses: current guides
        # use the REPLACE_MODEL_SERVER_IMAGE placeholder, older ones the xpu name.
        image_path.write_text(
            f"{image_config.rstrip()}\n\nimages:\n"
            f"  - name: REPLACE_MODEL_SERVER_IMAGE\n    newName: {image_name}\n    newTag: {image_tag}\n"
            f"  - name: ghcr.io/llm-d/llm-d-xpu\n    newName: {image_name}\n    newTag: {image_tag}\n",
            encoding="utf-8",
        )
        claim = claim_path.read_text(encoding="utf-8")
        if not re.search(r"(?m)^\s*count:\s*\d+", claim):
            raise ValueError("registered resource claim template is missing its card count")
        claim_data = yaml.safe_load(claim)
        gpu_request = claim_data["spec"]["spec"]["devices"]["requests"][0]
        gpu_request["exactly"]["count"] = deployment["accelerator_count"]
        selectors = gpu_device_selectors()
        if selectors:
            gpu_request["exactly"]["selectors"] = selectors
        claim_path.write_text(yaml.safe_dump(claim_data, sort_keys=False), encoding="utf-8")
        return rendered_overlay.resolve()

    def _deployment_parameters(self, overrides: dict[str, Any]) -> tuple[dict[str, int | None], str, dict[str, Any]]:
        """Map Configuration content to the registered Guide's render parameters."""
        model = overrides.get("model")
        decode = overrides.get("decode")
        if not isinstance(model, dict) or not isinstance(model.get("name"), str) or not model["name"]:
            raise ValueError("configuration content requires model.name")
        if not isinstance(decode, dict):
            raise ValueError("optimized-baseline configuration requires decode values")
        runtime = overrides.get("runtime")
        if not isinstance(runtime, dict):
            raise ValueError("configuration content requires runtime.image")
        source_document = self._configuration_source_document(runtime)
        source_content = source_document.get("deployable_configuration", source_document) if source_document else None
        if source_content is not None:
            if not isinstance(source_content, dict):
                raise ValueError("configuration source must contain a deployable configuration object")
            source_content = source_content.get("content", source_content)
            if not isinstance(source_content, dict):
                raise ValueError("configuration source content must be an object")
            model = source_content.get("model")
            decode = source_content.get("decode")
            if not isinstance(model, dict) or not isinstance(model.get("name"), str) or not model["name"]:
                raise ValueError("configuration source requires content.model.name")
            if not isinstance(decode, dict):
                raise ValueError("configuration source requires content.decode")
        image = runtime.get("image")
        image_mode = runtime.get("imageMode")
        if not isinstance(image, str) or not image or not isinstance(image_mode, str):
            raise ValueError("runtime.image and runtime.imageMode are required")
        _image_parts(image)
        build_source_url = runtime.get("buildSourceUrl")
        mount_path = runtime.get("mountPath", "")
        model_source = runtime.get("modelSource") or ("shared-path" if mount_path else "huggingface")
        if not isinstance(mount_path, str):
            raise ValueError("runtime.mountPath must be a string")
        if mount_path and (not mount_path.startswith("/") or "\n" in mount_path or "\r" in mount_path):
            raise ValueError("runtime.mountPath must be an absolute host path")
        if image_mode == "build-from-source":
            if not isinstance(build_source_url, str) or not re.fullmatch(
                r"https?://[^\s]+(?:\.git)?", build_source_url
            ):
                raise ValueError("runtime.buildSourceUrl must be an HTTP(S) Git URL")
        elif image_mode != "use-upstream-image":
            raise ValueError("runtime.imageMode must be use-upstream-image or build-from-source")
        replicas = decode.get("replicaCount")
        tensor_parallel_size = decode.get("tensorParallelSize")
        if isinstance(replicas, bool) or not isinstance(replicas, int):
            raise ValueError("decode.replicaCount must be an integer")
        if isinstance(tensor_parallel_size, bool) or not isinstance(tensor_parallel_size, int):
            raise ValueError("decode.tensorParallelSize must be an integer")
        return (
            {
                "replicas": replicas,
                "tensor_parallel_size": tensor_parallel_size,
                "accelerator_count": tensor_parallel_size,
                "max_num_seqs": _optional_positive_integer(decode, "maxNumSeqs"),
                "max_model_len": _optional_positive_integer(decode, "maxModelLen"),
            },
            model["name"],
            {
                "image": image,
                "image_mode": image_mode,
                "build_source_url": build_source_url or "",
                "mount_host_path": mount_path,
                "mounted_model_path": "/model-cache" if mount_path and model_source == "shared-path" else "",
                "model_source": model_source,
                "configuration_source_document": source_document,
                "environment": (
                    dict(runtime.get("environment")) if isinstance(runtime.get("environment"), dict) else {}
                ),
                "official_guide": overrides.get("officialGuide")
                if isinstance(overrides.get("officialGuide"), dict)
                else None,
            },
        )

    def _configuration_source_document(self, runtime: dict[str, Any]) -> dict[str, Any] | None:
        source = runtime.get("configurationSource")
        if source is None:
            return None
        if not isinstance(source, dict):
            raise ValueError("runtime.configurationSource must be an object")
        mode = source.get("mode")
        if mode == "suggested-yaml":
            file_path = source.get("filePath")
            expected_path = (
                self._policy.guide_root / "guides/optimized-baseline/modelserver/xpu/vllm/patch-vllm.yaml"
            ).resolve()
            if (
                file_path != "llm-d/guides/optimized-baseline/modelserver/xpu/vllm/patch-vllm.yaml"
                or not expected_path.is_file()
            ):
                raise ValueError("suggested deployment YAML is not available from the registered Guide")
            return None
        if mode == "generated-file":
            file_path = source.get("filePath")
            if not isinstance(file_path, str) or not file_path.startswith("/configs/"):
                raise ValueError("generated configuration source must use a controlled /configs path")
            file_name = file_path.removeprefix("/configs/")
            if not file_name or ".." in Path(file_name).parts or Path(file_name).is_absolute():
                raise ValueError("generated configuration source path is invalid")
            path = (CONFIGURATION_OUTPUT_DIR / file_name).resolve()
            if not path.is_relative_to(CONFIGURATION_OUTPUT_DIR.resolve()) or not path.is_file():
                raise ValueError("generated configuration source was not found")
            raw = path.read_text(encoding="utf-8")
        elif mode == "upload-yaml":
            raw = source.get("content")
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("uploaded configuration source must contain YAML content")
        else:
            raise ValueError("unsupported configuration source mode")
        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError as error:
            raise ValueError(f"configuration source is not valid YAML: {error}") from error
        if not isinstance(document, dict):
            raise ValueError("configuration source must be a YAML object")
        return document

    async def _build_image(self, source_url: str, target_image: str) -> None:
        docker_path = self._policy.docker_path
        if docker_path is None or not docker_path.is_file():
            raise ValueError("Docker is required to build an image from source")
        process = await asyncio.create_subprocess_exec(
            str(docker_path),
            "build",
            "--tag",
            target_image,
            source_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._policy.readiness_timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise ValueError("Docker image build timed out") from None
        if process.returncode != 0:
            raise ValueError(f"Docker image build failed: {stderr.decode(errors='replace').strip()}")

    def _namespace(self, execution_context: dict[str, Any]) -> str:
        namespace = str(execution_context.get("namespace") or "")
        if not namespace.startswith(self._policy.namespace_prefix) or namespace == self._policy.namespace_prefix:
            raise ValueError("deployment namespace is outside the guide execution allowlist")
        return namespace

    def _inject_model_environment(self, patch: str, runtime_environment: dict[str, Any]) -> str:
        values = {**(self._policy.proxy_environment or {}), **runtime_environment}
        values = {name: value for name, value in values.items() if isinstance(value, str) and value}
        if any(name in values for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")):
            # A proxy is in play, so the in-cluster bypass entries must always be
            # present: a caller-provided NO_PROXY overrides the ones
            # ``_model_environment`` adds, which would send cluster-internal
            # (<svc>.cluster.local) traffic to the proxy and fail it.
            entries: list[str] = []
            for entry in f"{values.get('NO_PROXY', '')},{values.get('no_proxy', '')}".split(","):
                entry = entry.strip()
                if entry and entry not in entries:
                    entries.append(entry)
            for entry in ("localhost", "127.0.0.1", ".svc", ".svc.cluster.local", ".cluster.local"):
                if entry not in entries:
                    entries.append(entry)
            merged_no_proxy = ",".join(entries)
            values["NO_PROXY"] = merged_no_proxy
            values["no_proxy"] = merged_no_proxy
        if not values:
            return patch
        environment = "\n".join(
            f"            - name: {name}\n              value: {value!r}" for name, value in sorted(values.items())
        )
        marker = "          env:\n"
        if marker not in patch:
            raise ValueError("registered vLLM patch is missing the modelserver environment section")
        return patch.replace(marker, f"{marker}{environment}\n", 1)

    @staticmethod
    def _inject_model_cache_mount(patch: str, host_path: str, *, read_only: bool = True) -> str:
        volume_mount_marker = "          volumeMounts:\n"
        volumes_marker = "      volumes:\n"
        if volume_mount_marker not in patch or volumes_marker not in patch:
            raise ValueError("registered vLLM patch is missing volume sections")
        patch = patch.replace(
            volume_mount_marker,
            f"{volume_mount_marker}            - mountPath: /model-cache\n"
            "              name: model-cache\n"
            f"              readOnly: {str(read_only).lower()}\n",
            1,
        )
        return patch.replace(
            volumes_marker,
            f"{volumes_marker}        - name: model-cache\n"
            "          hostPath:\n"
            f"            path: {host_path}\n"
            "            type: DirectoryOrCreate\n",
            1,
        )

    @staticmethod
    def _inject_model_cache_volume(patch: str, volume_source: dict[str, Any], *, read_only: bool = True) -> str:
        """Generalized ``_inject_model_cache_mount`` for a Storage-resolved volume.

        Unlike ``_inject_model_cache_mount`` (hostPath only), this accepts any
        Pod ``volumes[]`` body (``persistentVolumeClaim``, ``nfs``, ``hostPath``,
        ...) so a new storage type needs no change here.
        """
        volume_mount_marker = "          volumeMounts:\n"
        volumes_marker = "      volumes:\n"
        if volume_mount_marker not in patch or volumes_marker not in patch:
            raise ValueError("registered vLLM patch is missing volume sections")
        patch = patch.replace(
            volume_mount_marker,
            f"{volume_mount_marker}            - mountPath: /model-cache\n"
            "              name: model-cache\n"
            f"              readOnly: {str(read_only).lower()}\n",
            1,
        )
        dumped = yaml.safe_dump({"name": "model-cache", **volume_source}, sort_keys=False).rstrip("\n")
        lines = dumped.splitlines()
        volume_block = "\n".join([f"        - {lines[0]}", *(f"          {line}" for line in lines[1:])]) + "\n"
        return patch.replace(volumes_marker, f"{volumes_marker}{volume_block}", 1)


def _tree_checksum(path: Path) -> dict[str, str]:
    return {
        str(item.relative_to(path)): stable_hash({"content": item.read_text(encoding="utf-8")})
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _optional_positive_integer(values: dict[str, Any], name: str) -> int | None:
    value = values.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"decode.{name} must be a positive integer")
    return value


def _image_parts(image: str) -> tuple[str, str]:
    name, separator, tag = image.rpartition(":")
    if not separator or not name or not tag or "/" in tag or "@" in image:
        raise ValueError("runtime.image must include a repository and tag")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9./_-]*", name) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", tag):
        raise ValueError("runtime.image contains unsupported characters")
    return name, tag
