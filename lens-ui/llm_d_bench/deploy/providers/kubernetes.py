"""Controlled Kubernetes runtime for explicitly registered guide artifacts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_d_bench.common.hashing import stable_hash
from llm_d_bench.deploy.providers.guide_adapter import (
    GuideAdapter,
    GuideDefinition,
    GuideDeploymentArtifact,
    ValidationResult,
)

CommandRunner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


@dataclass(frozen=True)
class KubernetesExecutionPolicy:
    """Namespace and readiness constraints for one registered guide."""

    namespace_prefix: str = "llm-d-bench-"
    readiness_timeout_seconds: int = 300
    readiness_deployment_name: str | None = None


class KubernetesGuideAdapter(GuideAdapter):
    """Deploy a pre-rendered artifact through an injected structured runner."""

    def __init__(
        self,
        definition: GuideDefinition,
        manifest_path: Path,
        command_runner: CommandRunner,
        *,
        policy: KubernetesExecutionPolicy | None = None,
    ) -> None:
        self._definition = definition
        self._manifest_path = manifest_path
        self._command_runner = command_runner
        self._policy = policy or KubernetesExecutionPolicy()

    def discover(self) -> GuideDefinition:
        return self._definition

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
        # Capacity is advisory: Kubernetes may keep the requested pods Pending.
        if not self._manifest_path.is_file():
            return ValidationResult(accepted=False, reasons=["guide_manifest_missing"])
        return ValidationResult(accepted=True)

    async def render(self, definition: GuideDefinition, overrides: dict[str, Any]) -> GuideDeploymentArtifact:
        if definition.guide_id != self._definition.guide_id:
            raise ValueError("adapter cannot render a different guide")
        manifest = self._manifest_path.read_bytes()
        manifest_checksum = stable_hash({"manifest": manifest.hex()})
        values_checksum = stable_hash({"overrides": overrides})
        return GuideDeploymentArtifact(
            guide_id=definition.guide_id,
            artifact_hash=stable_hash(
                {
                    "guide": definition.content_hash,
                    "manifest_checksum": manifest_checksum,
                    "overrides": overrides,
                    "source_ref": definition.source_ref,
                }
            ),
            manifest_ref=str(self._manifest_path),
            values_checksum=values_checksum,
            source_ref=definition.source_ref,
            guide_content_hash=definition.content_hash,
            manifest_checksum=manifest_checksum,
        )

    async def deploy(self, artifact: GuideDeploymentArtifact, execution_context: dict[str, Any]) -> dict[str, Any]:
        self._validate_artifact(artifact)
        namespace = self._namespace(execution_context)
        status, stdout, stderr = await self._command_runner(["kubectl", "create", "namespace", namespace])
        if status != 0:
            raise RuntimeError((stderr or stdout or "kubectl namespace creation failed").strip())
        status, stdout, stderr = await self._command_runner(
            ["kubectl", "apply", "--namespace", namespace, "--filename", artifact.manifest_ref or ""],
        )
        if status != 0:
            raise RuntimeError((stderr or stdout or "kubectl apply failed").strip())
        execution = {"namespace": namespace, "artifact_hash": artifact.artifact_hash, "apply_output": stdout}
        take_evidence_refs = getattr(self._command_runner, "take_evidence_refs", None)
        if callable(take_evidence_refs):
            execution["evidence_refs"] = take_evidence_refs()
        return execution

    async def readiness(self, execution: dict[str, Any]) -> ValidationResult:
        namespace = str(execution["namespace"])
        timeout = f"--timeout={self._policy.readiness_timeout_seconds}s"
        deployment_name = self._policy.readiness_deployment_name
        if not deployment_name:
            return ValidationResult(accepted=False, reasons=["guide readiness deployment is not registered"])
        status, stdout, stderr = await self._command_runner(
            [
                "kubectl",
                "rollout",
                "status",
                f"deployment/{deployment_name}",
                "--namespace",
                namespace,
                timeout,
            ],
        )
        reasons = [] if status == 0 else [(stderr or stdout or "deployment_not_ready").strip()]
        if status != 0:
            return ValidationResult(accepted=False, reasons=reasons)
        try:
            endpoint = await self._discover_endpoint(namespace)
        except RuntimeError as error:
            return ValidationResult(accepted=False, reasons=[str(error)])
        if endpoint is not None:
            execution["endpoint_url"] = endpoint
        return ValidationResult(accepted=True)

    async def _discover_endpoint(self, namespace: str) -> str | None:
        service_name = self._definition.capabilities.get("endpoint_service_name")
        service_port = self._definition.capabilities.get("endpoint_service_port")
        if not service_name or not service_port:
            return None
        status, stdout, stderr = await self._command_runner(
            [
                "kubectl",
                "get",
                "service",
                str(service_name),
                "--namespace",
                namespace,
                "--output",
                "jsonpath={.spec.clusterIP}",
            ]
        )
        cluster_ip = stdout.strip()
        if status != 0 or not cluster_ip or cluster_ip == "None":
            raise RuntimeError((stderr or stdout or "deployment endpoint discovery failed").strip())
        return f"http://{service_name}.{namespace}.svc:{service_port}"

    async def rollback(self, execution: dict[str, Any], artifact: GuideDeploymentArtifact) -> dict[str, Any]:
        namespace = str(execution["namespace"])
        deployment_name = self._policy.readiness_deployment_name
        if not deployment_name:
            return {"rolled_back": False, "output": "", "error": "guide readiness deployment is not registered"}
        status, stdout, stderr = await self._command_runner(
            ["kubectl", "rollout", "undo", f"deployment/{deployment_name}", "--namespace", namespace],
        )
        return {"rolled_back": status == 0, "output": stdout, "error": stderr or None}

    async def stop(self, execution: dict[str, Any], artifact: GuideDeploymentArtifact) -> dict[str, Any]:
        namespace = self._namespace(execution)
        deployment_name = self._policy.readiness_deployment_name
        status, stdout, stderr = await self._command_runner(
            ["kubectl", "scale", f"deployment/{deployment_name}", "--namespace", namespace, "--replicas=0"],
        )
        return {"stopped": status == 0, "output": stdout, "error": stderr or None}

    async def cleanup(
        self, execution: dict[str, Any], artifact: GuideDeploymentArtifact, *, force: bool = False
    ) -> dict[str, Any]:
        namespace = str(execution["namespace"])
        status, stdout, stderr = await self._command_runner(
            ["kubectl", "delete", "namespace", namespace, "--ignore-not-found=true"],
        )
        return {"cleaned_up": status == 0, "output": stdout, "error": stderr or None}

    def _validate_artifact(self, artifact: GuideDeploymentArtifact) -> None:
        if artifact.guide_id != self._definition.guide_id:
            raise ValueError("artifact guide identity does not match registered adapter")
        source_matches = artifact.source_ref == self._definition.source_ref
        content_matches = artifact.guide_content_hash == self._definition.content_hash
        if not source_matches or not content_matches:
            raise ValueError("guide source drift detected before deployment")
        if artifact.manifest_ref != str(self._manifest_path):
            raise ValueError("artifact manifest reference drift detected before deployment")
        manifest_checksum = stable_hash({"manifest": self._manifest_path.read_bytes().hex()})
        if artifact.manifest_checksum != manifest_checksum:
            raise ValueError("artifact manifest content drift detected before deployment")

    def _namespace(self, execution_context: dict[str, Any]) -> str:
        namespace = str(execution_context.get("namespace") or "")
        if not namespace.startswith(self._policy.namespace_prefix) or namespace == self._policy.namespace_prefix:
            raise ValueError("deployment namespace is outside the guide execution allowlist")
        return namespace
