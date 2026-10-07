"""Descriptor-driven Helm plus Kustomize Guide deployment adapter."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from llm_d_bench.common.hashing import stable_hash
from llm_d_bench.deploy.data_plane import router_data_plane_args, router_data_plane_effective_values
from llm_d_bench.deploy.providers.deployment_bundle import (
    install_deployment_bundle,
    subprocess_bundle_runner,
)
from llm_d_bench.deploy.providers.guide_adapter import GuideDefinition, GuideDeploymentArtifact, ValidationResult
from llm_d_bench.deploy.providers.hardware_profile import requires_dra_claim, set_accelerator_request
from llm_d_bench.deploy.providers.model_cache_environment import model_cache_environment
from llm_d_bench.utils.paths import prism_temp_root
from llm_d_bench.utils.shell import spawn


@dataclass(frozen=True)
class HelmKustomizeGuideDescriptor:
    guide_id: str
    guide_root: Path
    variants: dict[str, Path]
    default_variant: str
    router_chart: str
    router_version: str
    router_values: tuple[Path, ...]
    release_name: str
    readiness_deployments: tuple[str, ...]
    endpoint_service: str
    endpoint_port: int
    #: Static data plane the guide declares: "llm-d-router" routes non-evaluation
    #: deployments through the shared Gateway; "external" leaves the provider alone.
    data_plane_kind: str = "external"
    log_selector: str = "llm-d.ai/role=decode"
    log_container: str = "modelserver"


class HelmKustomizeGuideAdapter:
    """Deploy only descriptor-registered overlays and fixed Helm plans."""

    published_manifest_lifecycle = True

    def __init__(
        self,
        descriptor: HelmKustomizeGuideDescriptor,
        command_runner,
        kubectl_path: Path,
        helm_path: Path,
        namespace_prefix: str,
        timeout_seconds: int,
        kubeconfig: str | None,
        accelerator: str | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._runner = command_runner
        self._kubectl_path = kubectl_path
        self._helm_path = helm_path
        self._namespace_prefix = namespace_prefix
        self._timeout = timeout_seconds
        # Resolved once per adapter instance (one per deployment run) from the
        # run's own accelerator, never a shared global: concurrent runs for
        # different clusters/vendors must not race on a single hardware choice.
        self._accelerator = accelerator
        self._environment = {**os.environ, "KUBECONFIG": kubeconfig} if kubeconfig else None
        self._bundle_command_runner = subprocess_bundle_runner(helm_path, kubectl_path, self._environment)
        self._render_root = prism_temp_root("prism-helm-kustomize", descriptor.guide_id)
        self._definition = GuideDefinition(
            guide_id=descriptor.guide_id,
            source_ref=f"local-{descriptor.guide_id}",
            content_hash=stable_hash(
                {
                    "variants": {name: str(path.resolve()) for name, path in descriptor.variants.items()},
                    "router_values": [str(path.resolve()) for path in descriptor.router_values],
                }
            ),
            maturity="supported-extension",
            capabilities={
                "adapter": "helm-kustomize",
                "variants": sorted(descriptor.variants),
                "default_variant": descriptor.default_variant,
                "endpoint_service_name": descriptor.endpoint_service,
                "endpoint_service_port": descriptor.endpoint_port,
            },
        )

    def discover(self) -> GuideDefinition:
        return self._definition

    def register_restored_namespace(self, namespace: str) -> None:
        register = getattr(self._runner, "register_restored_namespace", None)
        if callable(register):
            register(namespace)

    def validate_inputs(
        self, definition, cluster_snapshot: dict[str, Any], overrides: dict[str, Any]
    ) -> ValidationResult:
        try:
            parameters = self._parameters(overrides)
            overlay = self._descriptor.variants[parameters["variant"]]
            if not overlay.is_dir():
                raise ValueError("registered Guide variant overlay is unavailable")
            for values in self._descriptor.router_values:
                if not values.is_file():
                    raise ValueError("registered Guide router values are unavailable")
        except (KeyError, ValueError) as error:
            return ValidationResult(False, [str(error)])
        return ValidationResult(True)

    async def render(self, definition, overrides: dict[str, Any]) -> GuideDeploymentArtifact:
        parameters = self._parameters(overrides)
        overlay = self._descriptor.variants[parameters["variant"]]
        process = await spawn(
            [str(self._kubectl_path), "kustomize", str(overlay)],
            env=self._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise ValueError(f"Kustomize render failed: {stderr.decode(errors='replace').strip()}")
        documents = [item for item in yaml.safe_load_all(stdout) if isinstance(item, dict)]
        self._patch_documents(documents, parameters)
        digest = stable_hash({"parameters": parameters, "source": stdout.decode(errors="replace")})
        directory = self._render_root / digest
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        manifest = directory / "manifest.yaml"
        manifest.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
        return GuideDeploymentArtifact(
            guide_id=self._definition.guide_id,
            artifact_hash=digest,
            manifest_ref=str(manifest),
            source_ref=self._definition.source_ref,
            guide_content_hash=self._definition.content_hash,
            manifest_checksum=stable_hash({"content": manifest.read_text(encoding="utf-8")}),
            values_checksum=stable_hash(
                {"values": [path.read_text(encoding="utf-8") for path in self._descriptor.router_values]}
            ),
            deployment_contract={"data_plane_kind": self._descriptor.data_plane_kind},
        )

    def _data_plane_kind(self) -> str:
        # Tolerate adapters built without __init__ (tests use __new__).
        return getattr(getattr(self, "_descriptor", None), "data_plane_kind", "external")

    def _data_plane_args(self, execution_context) -> list[str]:
        if self._data_plane_kind() != "llm-d-router":
            return []
        return router_data_plane_args(execution_context)

    def _data_plane_effective_values(self, content: str, execution_context) -> str:
        if self._data_plane_kind() != "llm-d-router":
            return content
        return router_data_plane_effective_values(content, execution_context)

    async def deploy(self, artifact, execution_context):
        namespace = self._namespace(execution_context)
        status, stdout, stderr = await self._runner(["kubectl", "create", "namespace", namespace])
        if status != 0 and "AlreadyExists" not in stderr:
            raise RuntimeError((stderr or stdout).strip())
        bundle = artifact.deployment_contract.get("deploymentBundle")
        reproducibility = {}
        if bundle is not None:
            reproducibility = await install_deployment_bundle(
                bundle,
                guide=artifact.guide_id,
                source_commit=str(artifact.source_ref or ""),
                namespace=namespace,
                command_runner=self._bundle_command_runner,
                output_root=Path(artifact.manifest_ref or "").parent / "deployment-bundle",
                effective_values_content=self._data_plane_effective_values(
                    bundle["helm"]["values"][-1]["content"], execution_context
                ),
                effective_values_name=f"router-effective-{artifact.guide_id}.yaml",
            )
        else:
            helm_command = [
                str(self._helm_path),
                "upgrade",
                "--install",
                self._descriptor.release_name,
                self._descriptor.router_chart,
                "--namespace",
                namespace,
                "--version",
                self._descriptor.router_version,
            ]
            for values in self._descriptor.router_values:
                helm_command.extend(["--values", str(values)])
            helm_command.extend(self._data_plane_args(execution_context))
            helm = await spawn(
                helm_command,
                env=self._environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            helm_stdout, helm_stderr = await helm.communicate()
            if helm.returncode:
                raise RuntimeError((helm_stderr or helm_stdout).decode(errors="replace").strip())
        status, stdout, stderr = await self._runner(
            [
                "kubectl",
                "apply",
                "--namespace",
                namespace,
                "--filename",
                artifact.manifest_ref or "",
            ]
        )
        if status != 0:
            raise RuntimeError((stderr or stdout).strip())
        return {
            "namespace": namespace,
            "artifact_hash": artifact.artifact_hash,
            "apply_output": stdout,
            **reproducibility,
        }

    async def readiness(self, execution):
        namespace = execution["namespace"]
        for deployment in self._descriptor.readiness_deployments:
            status, stdout, stderr = await self._runner(
                [
                    "kubectl",
                    "rollout",
                    "status",
                    f"deployment/{deployment}",
                    "--namespace",
                    namespace,
                    f"--timeout={self._timeout}s",
                ]
            )
            if status != 0:
                return ValidationResult(False, [(stderr or stdout or "deployment_not_ready").strip()])
        execution["endpoint_url"] = (
            f"http://{self._descriptor.endpoint_service}.{namespace}.svc:{self._descriptor.endpoint_port}"
        )
        return ValidationResult(True)

    async def diagnostics(self, execution):
        namespace = execution["namespace"]

        async def capture(command):
            status, stdout, stderr = await self._runner(command)
            return (stdout if status == 0 else stderr).strip()

        return {
            "namespace": namespace,
            "pods": await capture(["kubectl", "get", "pods", "--namespace", namespace, "-o", "wide"]),
            "events": await capture(["kubectl", "get", "events", "--namespace", namespace, "--sort-by=.lastTimestamp"]),
            "modelserver_logs": await capture(
                [
                    "kubectl",
                    "logs",
                    "--namespace",
                    namespace,
                    "-l",
                    self._descriptor.log_selector,
                    "-c",
                    self._descriptor.log_container,
                    "--tail=120",
                ]
            ),
            **{
                key: value
                for key, value in execution.items()
                if key.startswith("router_effective_") or key.startswith("router_rendered_")
            },
            "captured_at": datetime.now(UTC).isoformat(),
        }

    async def stop(self, execution, artifact):
        outputs = []
        stopped = True
        for deployment in self._descriptor.readiness_deployments:
            status, stdout, stderr = await self._runner(
                [
                    "kubectl",
                    "scale",
                    f"deployment/{deployment}",
                    "--namespace",
                    execution["namespace"],
                    "--replicas=0",
                ]
            )
            stopped = stopped and status == 0
            outputs.append(stderr or stdout)
        return {"stopped": stopped, "output": "\n".join(outputs), "error": None}

    async def rollback(self, execution, artifact):
        return await self.stop(execution, artifact)

    async def cleanup(self, execution, artifact, *, force=False):
        helm = await spawn(
            [str(self._helm_path), "uninstall", self._descriptor.release_name, "--namespace", execution["namespace"]],
            env=self._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await helm.communicate()
        status, stdout, stderr = await self._runner(
            [
                "kubectl",
                "delete",
                "namespace",
                execution["namespace"],
                "--ignore-not-found=true",
            ]
        )
        if artifact.manifest_ref:
            shutil.rmtree(Path(artifact.manifest_ref).parent, ignore_errors=True)
        return {"cleaned_up": status == 0, "output": stdout, "error": stderr or None}

    def _parameters(self, overrides: dict[str, Any]) -> dict[str, Any]:
        model = overrides.get("model") or {}
        decode = overrides.get("decode") or {}
        runtime = overrides.get("runtime") or {}
        variant = str(overrides.get("guideVariant") or self._descriptor.default_variant)
        if variant not in self._descriptor.variants:
            raise ValueError(f"unsupported {self._descriptor.guide_id} variant: {variant}")
        if not isinstance(model.get("name"), str) or not model["name"]:
            raise ValueError("Guide configuration requires model.name")
        image = runtime.get("image")
        if not isinstance(image, str) or ":" not in image:
            raise ValueError("Guide configuration requires a tagged runtime.image")
        replicas, tensor_parallel = decode.get("replicaCount"), decode.get("tensorParallelSize")
        if not isinstance(replicas, int) or replicas < 1 or not isinstance(tensor_parallel, int) or tensor_parallel < 1:
            raise ValueError("Guide configuration requires positive decode replicaCount and tensorParallelSize")
        return {
            "variant": variant,
            "model": model["name"],
            "image": image,
            "mount_path": runtime.get("mountPath") or "",
            "replicas": replicas,
            "tensor_parallel_size": tensor_parallel,
            "max_model_len": decode.get("maxModelLen") or model.get("maxModelLen"),
            "environment": runtime.get("environment") if isinstance(runtime.get("environment"), dict) else {},
            "custom_parameters": overrides.get("customParameters") or [],
        }

    def _patch_documents(self, documents: list[dict[str, Any]], parameters: dict[str, Any]) -> None:
        deployment = next((item for item in documents if item.get("kind") == "Deployment"), None)
        claim = next((item for item in documents if item.get("kind") == "ResourceClaimTemplate"), None)
        if deployment is None or (requires_dra_claim(accelerator=self._accelerator) and claim is None):
            raise ValueError("rendered Guide is missing Deployment or ResourceClaimTemplate")
        deployment["spec"]["replicas"] = parameters["replicas"]
        # XPU model initialization can legitimately take longer than Kubernetes'
        # default ten-minute Deployment progress deadline, especially while a DRA
        # claim is being prepared.
        deployment["spec"]["progressDeadlineSeconds"] = 1800
        container = next(
            item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "modelserver"
        )
        container["image"] = parameters["image"]
        command = container.get("args", [""])[0]
        model = "/model-cache" if parameters["mount_path"] else parameters["model"]
        command = re.sub(r"(?m)(exec vllm serve\s+\\?\n?\s*)(\S+)", rf"\g<1>{model}", command, count=1)
        command = HelmKustomizeGuideAdapter._set_shell_argument(
            command, "tensor-parallel-size", parameters["tensor_parallel_size"]
        )
        if parameters["max_model_len"]:
            command = HelmKustomizeGuideAdapter._set_shell_argument(
                command, "max-model-len", parameters["max_model_len"]
            )
        for custom in parameters["custom_parameters"]:
            if custom.get("target") not in {"decode", "both"}:
                continue
            if custom.get("kind") == "argument":
                command = HelmKustomizeGuideAdapter._set_shell_argument(command, custom["name"], custom["value"])
            else:
                environment = container.setdefault("env", [])
                existing = next((item for item in environment if item.get("name") == custom["name"]), None)
                if existing:
                    existing.clear()
                    existing.update({"name": custom["name"], "value": custom["value"]})
                else:
                    environment.append({"name": custom["name"], "value": custom["value"]})
        for name, value in parameters["environment"].items():
            if value:
                environment = container.setdefault("env", [])
                existing = next((item for item in environment if item.get("name") == name), None)
                if existing:
                    existing.clear()
                    existing.update({"name": name, "value": value})
                else:
                    environment.append({"name": name, "value": value})
        for name, value in model_cache_environment("shared-path", cache_mounted=bool(parameters["mount_path"])).items():
            environment = container.setdefault("env", [])
            existing = next((item for item in environment if item.get("name") == name), None)
            if existing:
                existing.clear()
                existing.update({"name": name, "value": value})
            else:
                environment.append({"name": name, "value": value})
        container["args"] = [command]
        set_accelerator_request(container, claim, parameters["tensor_parallel_size"], accelerator=self._accelerator)
        if parameters["mount_path"]:
            pod_spec = deployment["spec"]["template"]["spec"]
            pod_spec.setdefault("volumes", []).append(
                {"name": "model-cache", "hostPath": {"path": parameters["mount_path"], "type": "DirectoryOrCreate"}}
            )
            container.setdefault("volumeMounts", []).append(
                {"name": "model-cache", "mountPath": "/model-cache", "readOnly": True}
            )

    @staticmethod
    def _set_shell_argument(command: str, name: str, value: object) -> str:
        pattern = rf"--{re.escape(name)}(?:=|\s+)\S+"
        replacement = f"--{name}={value}"
        if re.search(pattern, command):
            return re.sub(pattern, replacement, command, count=1)
        return f"{command.rstrip()} \\\n                {replacement}"

    def _namespace(self, context: dict[str, Any]) -> str:
        namespace = str(context.get("namespace") or "")
        if not namespace.startswith(self._namespace_prefix) or namespace == self._namespace_prefix:
            raise ValueError("deployment namespace is outside the configured allowlist")
        return namespace
