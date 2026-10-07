"""Plain Kubernetes vLLM baseline provider using Intel DRA devices."""

from __future__ import annotations

import asyncio
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from llm_d_bench.common.hashing import stable_hash
from llm_d_bench.deploy.providers.gpu_selection import gpu_device_selectors
from llm_d_bench.deploy.providers.guide_adapter import GuideDefinition, GuideDeploymentArtifact, ValidationResult
from llm_d_bench.deploy.providers.hardware_profile import claim_request_name, device_class
from llm_d_bench.deploy.providers.model_cache_environment import model_cache_environment
from llm_d_bench.deploy.providers.storage_mount import resolve_mount
from llm_d_bench.utils.paths import prism_temp_root


class BaselineVllmAdapter:
    def __init__(
        self, command_runner, namespace_prefix: str, readiness_timeout_seconds: int, docker_path: Path | None
    ) -> None:
        self._runner = command_runner
        self._namespace_prefix = namespace_prefix
        self._timeout = readiness_timeout_seconds
        self._docker_path = docker_path
        self._root = prism_temp_root("prism-baseline-vllm")
        self._definition = GuideDefinition(
            guide_id="baseline-vllm",
            source_ref="generated-kubernetes-baseline",
            content_hash=stable_hash({"provider": "baseline-vllm.v1"}),
            maturity="supported-core",
            capabilities={
                "variant": "plain-kubernetes-vllm",
                "endpoint_service_name": "vllm",
                "endpoint_service_port": 8000,
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
            self._parameters(overrides)
        except ValueError as error:
            return ValidationResult(False, [str(error)])
        return ValidationResult(True)

    async def render(self, definition, overrides: dict[str, Any]) -> GuideDeploymentArtifact:
        parameters = self._parameters(overrides)
        parameters["resolved_mount"] = await resolve_mount(
            overrides.get("runtime") or {},
            cluster_id=overrides.get("_cluster_id"),
            namespace=overrides.get("_deployment_namespace"),
        )
        if parameters["image_mode"] == "build-from-source":
            await self._build_image(parameters["build_source_url"], parameters["image"])
        digest = stable_hash(parameters)
        directory = self._root / digest
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        manifest = directory / "manifest.yaml"
        manifest.write_text(yaml.safe_dump_all(self._resources(parameters), sort_keys=False), encoding="utf-8")
        return GuideDeploymentArtifact(
            guide_id=self._definition.guide_id,
            artifact_hash=digest,
            manifest_ref=str(manifest),
            source_ref=self._definition.source_ref,
            guide_content_hash=self._definition.content_hash,
            manifest_checksum=stable_hash({"content": manifest.read_text(encoding="utf-8")}),
            # Plain vLLM Service, not an llm-d router: never uses the shared Gateway.
            deployment_contract={"data_plane_kind": "direct"},
        )

    async def deploy(self, artifact: GuideDeploymentArtifact, execution_context: dict[str, Any]) -> dict[str, Any]:
        namespace = self._namespace(execution_context)
        status, stdout, stderr = await self._runner(["kubectl", "create", "namespace", namespace])
        if status != 0 and "AlreadyExists" not in stderr:
            raise RuntimeError((stderr or stdout).strip())
        policy = execution_context.get("deployment_policy") or {}
        model_secret = policy.get("model_secret") or {}
        model_token = execution_context.get("model_token")
        if model_token:
            create_secret = getattr(self._runner, "create_model_secret_from_token", None)
            if not callable(create_secret):
                raise RuntimeError("deployment runner cannot provision the supplied model token")
            await create_secret(namespace, model_token)
        elif model_secret.get("mode") == "existing-secret":
            await self._runner.copy_model_secret(
                namespace,
                str(model_secret.get("sourceNamespace") or ""),
                str(model_secret.get("sourceName") or ""),
            )
        elif model_secret.get("mode") == "host" and self._parameters_from_artifact_path(artifact).get(
            "needs_token", True
        ):
            token_file = Path.home() / ".cache" / "huggingface" / "token"
            create_secret = getattr(self._runner, "create_model_secret", None)
            if callable(create_secret) and token_file.is_file():
                await create_secret(namespace, token_file)
        status, stdout, stderr = await self._runner(
            ["kubectl", "apply", "--namespace", namespace, "--filename", artifact.manifest_ref or ""]
        )
        if status != 0:
            raise RuntimeError((stderr or stdout).strip())
        return {"namespace": namespace, "artifact_hash": artifact.artifact_hash, "apply_output": stdout}

    async def readiness(self, execution: dict[str, Any]) -> ValidationResult:
        namespace = execution["namespace"]
        status, stdout, stderr = await self._runner(
            [
                "kubectl",
                "rollout",
                "status",
                "deployment/vllm",
                "--namespace",
                namespace,
                f"--timeout={self._timeout}s",
            ]
        )
        if status != 0:
            return ValidationResult(False, [(stderr or stdout or "deployment_not_ready").strip()])
        execution["endpoint_url"] = f"http://vllm.{namespace}.svc:8000"
        return ValidationResult(True)

    async def diagnostics(self, execution: dict[str, Any]) -> dict[str, Any]:
        namespace = execution["namespace"]

        async def capture(command):
            status, stdout, stderr = await self._runner(command)
            return (stdout if status == 0 else stderr).strip()

        status, pod_names, _stderr = await self._runner(
            [
                "kubectl",
                "get",
                "pods",
                "--namespace",
                namespace,
                "-l",
                "llm-d.ai/role=decode",
                "-o",
                "jsonpath={.items[*].metadata.name}",
            ]
        )
        logs = []
        if status == 0:
            for pod_name in pod_names.split():
                current = await capture(
                    ["kubectl", "logs", "--namespace", namespace, pod_name, "-c", "modelserver", "--tail=120"]
                )
                previous_status, previous_stdout, previous_stderr = await self._runner(
                    [
                        "kubectl",
                        "logs",
                        "--namespace",
                        namespace,
                        pod_name,
                        "-c",
                        "modelserver",
                        "--previous",
                        "--tail=120",
                    ]
                )
                previous = previous_stdout if previous_status == 0 else previous_stderr
                if previous_status != 0 and "previous terminated container" in previous and "not found" in previous:
                    previous = ""
                previous = previous.strip()
                output = "\n".join(part for part in (current, previous) if part)
                if output:
                    logs.append(f"[{pod_name}]\n{output}")

        return {
            "namespace": namespace,
            "pods": await capture(["kubectl", "get", "pods", "--namespace", namespace, "-o", "wide"]),
            "events": await capture(["kubectl", "get", "events", "--namespace", namespace, "--sort-by=.lastTimestamp"]),
            "modelserver_logs": "\n\n".join(logs),
            "captured_at": datetime.now(UTC).isoformat(),
        }

    async def stop(self, execution, artifact):
        status, stdout, stderr = await self._runner(
            [
                "kubectl",
                "scale",
                "deployment/vllm",
                "--namespace",
                execution["namespace"],
                "--replicas=0",
            ]
        )
        return {"stopped": status == 0, "output": stdout, "error": stderr or None}

    async def rollback(self, execution, artifact):
        return await self.stop(execution, artifact)

    async def cleanup(self, execution, artifact, *, force: bool = False):
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

    def _namespace(self, context: dict[str, Any]) -> str:
        namespace = str(context.get("namespace") or "")
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace):
            raise ValueError("deployment namespace is invalid")
        register_namespace = getattr(self._runner, "register_namespace", None)
        if callable(register_namespace):
            register_namespace(namespace)
        return namespace

    @staticmethod
    def _parameters(overrides: dict[str, Any]) -> dict[str, Any]:
        model = overrides.get("model") or {}
        decode = overrides.get("decode") or {}
        runtime = overrides.get("runtime") or {}
        if not isinstance(model.get("name"), str) or not model["name"]:
            raise ValueError("baseline requires model.name")
        image = runtime.get("image")
        if not isinstance(image, str) or ":" not in image:
            raise ValueError("baseline requires runtime.image with a tag")
        replicas = decode.get("replicaCount")
        tensor_parallel = decode.get("tensorParallelSize")
        if not isinstance(replicas, int) or replicas < 1 or not isinstance(tensor_parallel, int) or tensor_parallel < 1:
            raise ValueError("baseline requires positive decode replicaCount and tensorParallelSize")
        pvc_name = runtime.get("pvcName") or ""
        if not isinstance(pvc_name, str) or (
            pvc_name and not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", pvc_name)
        ):
            raise ValueError("baseline runtime.pvcName must be a valid Kubernetes PVC name")
        return {
            "model": model["name"],
            "image": image,
            "replicas": replicas,
            "tensor_parallel_size": tensor_parallel,
            "image_mode": runtime.get("imageMode"),
            "build_source_url": runtime.get("buildSourceUrl") or "",
            "mount_path": runtime.get("mountPath") or "",
            "pvc_name": pvc_name,
            "model_source": runtime.get("modelSource")
            or ("shared-path" if runtime.get("mountPath") else "huggingface"),
            "environment": runtime.get("environment") or {},
            "custom_parameters": overrides.get("customParameters") or [],
            "max_model_len": decode.get("maxModelLen"),
        }

    @staticmethod
    def _parameters_from_artifact_path(artifact: GuideDeploymentArtifact) -> dict[str, Any]:
        if not artifact.manifest_ref:
            return {}
        try:
            documents = list(yaml.safe_load_all(Path(artifact.manifest_ref).read_text(encoding="utf-8")))
            deployment = next(item for item in documents if isinstance(item, dict) and item.get("kind") == "Deployment")
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            mounts = container.get("volumeMounts") or []
            environment = container.get("env") or []
            return {
                "mount_path": next((item.get("mountPath") for item in mounts if item.get("name") == "model-cache"), ""),
                "needs_token": any(item.get("name") == "HF_TOKEN" for item in environment),
            }
        except (OSError, KeyError, StopIteration, TypeError, yaml.YAMLError):
            return {}

    async def _build_image(self, source: str, image: str) -> None:
        if self._docker_path is None or not self._docker_path.is_file():
            raise ValueError("Docker is required to build an image from source")
        process = await asyncio.create_subprocess_exec(
            str(self._docker_path),
            "build",
            "--tag",
            image,
            source,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode:
            raise ValueError(f"Docker image build failed: {stderr.decode(errors='replace').strip()}")

    @staticmethod
    def _resources(parameters: dict[str, Any]) -> list[dict[str, Any]]:
        resolved_mount = parameters.get("resolved_mount")
        active_model_source = resolved_mount["model_source"] if resolved_mount else parameters["model_source"]
        mount_active = bool(resolved_mount) or bool(parameters["mount_path"]) or bool(parameters["pvc_name"])
        served_model = "/model-cache" if active_model_source == "shared-path" and mount_active else parameters["model"]
        args = [served_model, "--port=8000", f"--tensor-parallel-size={parameters['tensor_parallel_size']}"]
        if isinstance(parameters.get("max_model_len"), int) and parameters["max_model_len"] > 0:
            args.append(f"--max-model-len={parameters['max_model_len']}")
        environment = [
            {"name": name, "value": str(value)} for name, value in sorted(parameters["environment"].items()) if value
        ]
        if active_model_source != "shared-path":
            environment.append(
                {
                    "name": "HF_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "llm-d-hf-token",
                            "key": "HF_TOKEN",
                            "optional": True,
                        }
                    },
                }
            )
        environment.extend(
            {"name": name, "value": value}
            for name, value in model_cache_environment(active_model_source, cache_mounted=mount_active).items()
        )
        for item in parameters["custom_parameters"]:
            if item.get("target") not in {"decode", "both"}:
                continue
            if item.get("kind") == "argument":
                name = item["name"]
                value = str(item["value"]).lower()
                if name in {"enable-auto-tool-choice", "enable-prefix-caching", "enforce-eager"} and value in {
                    "true",
                    "false",
                }:
                    args.append(f"--{name}" if value == "true" else f"--no-{name}")
                else:
                    args.append(f"--{name}={item['value']}")
            elif item.get("kind") == "environment":
                environment.append({"name": item["name"], "value": item["value"]})
        claim_name = f"{claim_request_name()}-claim"
        pod_spec: dict[str, Any] = {
            "enableServiceLinks": False,
            "containers": [
                {
                    "name": "modelserver",
                    "image": parameters["image"],
                    "command": ["vllm", "serve"],
                    "args": args,
                    # Named "modelserver" to match the PodMonitor's named-port
                    # selector (see monitoring/deployment/service.py); an unnamed
                    # port cannot be scraped by that PodMonitor at all.
                    "env": environment,
                    "ports": [{"name": "modelserver", "containerPort": 8000}],
                    "resources": {"claims": [{"name": claim_name}]},
                    "readinessProbe": {
                        "httpGet": {"path": "/health", "port": 8000},
                        "periodSeconds": 5,
                        "failureThreshold": 360,
                    },
                }
            ],
            "resourceClaims": [{"name": claim_name, "resourceClaimTemplateName": claim_name}],
        }
        if resolved_mount is not None:
            pod_spec["volumes"] = [{"name": "model-cache", **resolved_mount["volume_source"]}]
            pod_spec["containers"][0]["volumeMounts"] = [
                {
                    "name": "model-cache",
                    "mountPath": resolved_mount["mount_path"],
                    "readOnly": resolved_mount["read_only"],
                }
            ]
        elif parameters["mount_path"] or parameters["pvc_name"]:
            volume = (
                {"name": "model-cache", "persistentVolumeClaim": {"claimName": parameters["pvc_name"]}}
                if parameters["pvc_name"]
                else {
                    "name": "model-cache",
                    "hostPath": {"path": parameters["mount_path"], "type": "DirectoryOrCreate"},
                }
            )
            pod_spec["volumes"] = [volume]
            pod_spec["containers"][0]["volumeMounts"] = [
                {
                    "name": "model-cache",
                    "mountPath": "/model-cache",
                    "readOnly": parameters["model_source"] == "shared-path",
                }
            ]
            if parameters["model_source"] == "shared-path":
                pod_spec["containers"][0]["args"][0] = "/model-cache"
        labels = {"app": "vllm", "llm-d.ai/role": "decode", "prism.ai/evaluation-kind": "baseline"}
        gpu_request: dict[str, Any] = {
            "name": claim_request_name(),
            "exactly": {"deviceClassName": device_class(), "count": parameters["tensor_parallel_size"]},
        }
        selectors = gpu_device_selectors()
        if selectors:
            gpu_request["exactly"]["selectors"] = selectors
        return [
            {
                "apiVersion": "resource.k8s.io/v1",
                "kind": "ResourceClaimTemplate",
                "metadata": {"name": claim_name},
                "spec": {"spec": {"devices": {"requests": [gpu_request]}}},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "vllm"},
                "spec": {
                    "replicas": parameters["replicas"],
                    "progressDeadlineSeconds": 1800,
                    "selector": {"matchLabels": labels},
                    "template": {"metadata": {"labels": labels}, "spec": pod_spec},
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "vllm"},
                "spec": {"selector": labels, "ports": [{"name": "http", "port": 8000, "targetPort": 8000}]},
            },
        ]
