"""Explicit environment composition for the Deploy runtime."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from llm_d_bench.common.hashing import stable_hash
from llm_d_bench.deploy.providers.baseline_vllm import BaselineVllmAdapter
from llm_d_bench.deploy.providers.configuration_manifest import ConfigurationManifestAdapter
from llm_d_bench.deploy.providers.guide_adapter import GuideDefinition
from llm_d_bench.deploy.providers.guide_catalog import GuideCatalog
from llm_d_bench.deploy.providers.hardware_profile import overlay_variant
from llm_d_bench.deploy.providers.helm_kustomize import HelmKustomizeGuideAdapter, HelmKustomizeGuideDescriptor
from llm_d_bench.deploy.providers.kubernetes import KubernetesExecutionPolicy, KubernetesGuideAdapter
from llm_d_bench.deploy.providers.optimized_baseline import (
    OptimizedBaselineGuideAdapter,
    OptimizedBaselineGuidePolicy,
)
from llm_d_bench.deploy.providers.pd_disaggregation import PdDisaggregationAdapter
from llm_d_bench.deploy.providers.precise_prefix_cache_routing import PrecisePrefixCacheRoutingAdapter
from llm_d_bench.deploy.service import GuideAdapterDeploymentService
from llm_d_bench.utils.paths import storage_path
from llm_d_bench.utils.shell import run_sync, spawn
from llm_d_bench.versions import router_chart_version


class RuntimeConfigurationError(ValueError):
    """Raised when an explicitly enabled runtime has invalid configuration."""


def resolve_deployment_source(source: Mapping[str, object] | None) -> dict[str, object]:
    """Resolve a GitHub source URL to a checked-out local Guide tree."""
    resolved = dict(source or {})
    repository = str(resolved.get("repository") or "").strip()
    if not repository:
        return resolved
    github_match = re.fullmatch(
        r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repository>[A-Za-z0-9_.-]+?)(?:\.git)?/?",
        repository,
    )
    if github_match is None:
        return resolved
    repository_name = github_match.group("repository")
    checkout = storage_path("cache", "deploy-repos", github_match.group("owner"), repository_name)
    branch = str(resolved.get("branch") or "main").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch:
        raise RuntimeConfigurationError("deployment source branch is invalid")
    checkout.parent.mkdir(parents=True, exist_ok=True)
    if (checkout / ".git").is_dir():
        _run_git(checkout, "fetch", "origin", "--prune")
    elif checkout.exists():
        raise RuntimeConfigurationError(f"deployment source cache path is not a Git repository: {checkout}")
    else:
        _run_git(checkout.parent, "clone", repository, str(checkout))
    try:
        _run_git(checkout, "checkout", "--force", branch)
    except RuntimeConfigurationError:
        _run_git(checkout, "checkout", "--force", "--track", f"origin/{branch}")
    resolved.update(
        {
            "repository": repository,
            "branch": branch,
            "resolved_repository": str(checkout),
            "resolved_from": "github",
        }
    )
    return resolved


def _run_git(cwd: Path, *arguments: str) -> None:
    result = run_sync(["git", *arguments], cwd=cwd)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeConfigurationError(f"unable to prepare deployment source: {detail or 'git command failed'}")


DEFAULT_RUNTIME_ENVIRONMENT = {
    "LLM_D_BENCH_NAMESPACE_PREFIX": "llmd-",
    "LLM_D_BENCH_READINESS_TIMEOUT_SECONDS": "900",
    "LLM_D_BENCH_KUBECTL_TIMEOUT_SECONDS": "300",
    "LLM_D_BENCH_RETAIN_NAMESPACE_ON_FAILURE": "true",
}


@dataclass(frozen=True)
class RegisteredGuideRuntime:
    """Immutable deployment metadata for one server-approved Guide."""

    guide_id: str
    source_ref: str
    content_hash: str
    maturity: str
    manifest_path: Path
    variant: str | None = None
    readiness_deployment_name: str | None = None
    endpoint_service_name: str | None = None
    endpoint_service_port: int | None = None
    #: The model server Service a readiness smoke test can reach directly. In
    #: Gateway Mode the EPP proxy is disabled, so its Service port has no backend.
    baseline_service_name: str | None = None
    baseline_service_port: int | None = None
    adapter_type: str = "kubernetes"
    guide_root: Path | None = None
    router_base_values_path: Path | None = None
    router_values_path: Path | None = None
    neutral_router_values_path: Path | None = None
    router_chart: str | None = None
    router_chart_version: str | None = None
    router_release_name: str | None = None
    proxy_environment: dict[str, str] | None = None


@dataclass(frozen=True)
class CommandEvidence:
    """Non-secret command outcome retained as an execution evidence reference."""

    operation: str
    namespace: str
    status: int
    timed_out: bool
    output_checksum: str


@dataclass(frozen=True)
class RuntimeEnvironment:
    manifest_root: Path
    kubectl_path: Path
    helm_path: Path | None
    docker_path: Path | None
    namespace_prefix: str
    retain_namespace_on_failure: bool
    readiness_timeout_seconds: int
    command_timeout_seconds: int
    model_environment: dict[str, str]
    kubeconfig: str | None
    #: This run's resolved accelerator (the UI/provenance selection), used to
    #: pick the right vendor overlay. ``None`` keeps the historical Intel
    #: default; never read from the real process environment at render time,
    #: so concurrent runs for different clusters/vendors cannot race.
    accelerator: str | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> RuntimeEnvironment | None:
        values = {**DEFAULT_RUNTIME_ENVIRONMENT, **os.environ, **(environ or {})}
        if values.get("LLM_D_BENCH_DEPLOY_RUNTIME_ENABLED", "true").lower() in {"0", "false", "no"}:
            return None

        manifest_root_value = values.get("LLM_D_ROOT", "").strip()
        if not manifest_root_value:
            raise RuntimeConfigurationError("LLM_D_ROOT must point to an llm-d checkout containing guides/")
        manifest_root = Path(manifest_root_value).resolve()
        if not (manifest_root / "guides").is_dir():
            raise RuntimeConfigurationError("LLM_D_ROOT must point to an llm-d checkout containing guides/")
        kubectl_path = _required_executable_file(values, "LLM_D_BENCH_KUBECTL_PATH", "kubectl")
        helm_path = _optional_executable_file(values, "LLM_D_BENCH_HELM_PATH", "helm")
        docker_path = _optional_executable_file(values, "LLM_D_BENCH_DOCKER_PATH", "docker")

        namespace_prefix = values["LLM_D_BENCH_NAMESPACE_PREFIX"]
        if not namespace_prefix.endswith("-") or not namespace_prefix.replace("-", "").isalnum():
            raise RuntimeConfigurationError(
                "LLM_D_BENCH_NAMESPACE_PREFIX must be a lowercase alphanumeric prefix ending in '-'"
            )

        return cls(
            manifest_root=manifest_root,
            kubectl_path=kubectl_path,
            helm_path=helm_path,
            docker_path=docker_path,
            namespace_prefix=namespace_prefix,
            retain_namespace_on_failure=_enabled(values, "LLM_D_BENCH_RETAIN_NAMESPACE_ON_FAILURE"),
            readiness_timeout_seconds=_positive_integer(values, "LLM_D_BENCH_READINESS_TIMEOUT_SECONDS", 900),
            command_timeout_seconds=_positive_integer(values, "LLM_D_BENCH_KUBECTL_TIMEOUT_SECONDS", 120),
            model_environment=_model_environment(values),
            kubeconfig=values.get("KUBECONFIG") or None,
            accelerator=(values.get("PRISM_DEPLOY_ACCELERATOR") or "").strip() or None,
        )


class RestrictedKubectlRunner:
    """Run only the command shapes required by the registered Kubernetes adapter."""

    def __init__(
        self, kubectl_path: Path, namespace_prefix: str, timeout_seconds: int = 120, kubeconfig: str | None = None
    ) -> None:
        self._kubectl_path = kubectl_path
        self._namespace_prefix = namespace_prefix
        self._restored_namespaces: set[str] = set()
        self._registered_namespaces: set[str] = set()
        self._timeout_seconds = timeout_seconds
        self._environment = {**os.environ, "KUBECONFIG": kubeconfig} if kubeconfig else None
        self._evidence: list[CommandEvidence] = []

    def register_restored_namespace(self, namespace: str) -> None:
        self._restored_namespaces.add(namespace)

    def register_namespace(self, namespace: str) -> None:
        self._registered_namespaces.add(namespace)

    def allows_namespace(self, namespace: str) -> bool:
        return (
            namespace.startswith(self._namespace_prefix)
            or namespace in self._registered_namespaces
            or namespace in self._restored_namespaces
        )

    async def __call__(self, command: list[str]) -> tuple[int, str, str]:
        self._validate(command)
        from llm_d_bench.utils.kubernetes_commands import execute_sdk_command, sdk_enabled, sdk_writes_enabled

        if sdk_enabled():
            result = await execute_sdk_command(
                command,
                kubeconfig=(self._environment or os.environ).get("KUBECONFIG"),
                timeout=self._timeout_seconds,
                allow_writes=sdk_writes_enabled(),
            )
            if result is not None:
                safe_stdout = _redact_output(result.stdout.encode())
                safe_stderr = _redact_output(result.stderr.encode())
                self._record_evidence(
                    command,
                    result.returncode,
                    f"{safe_stdout}\n{safe_stderr}",
                    timed_out=result.returncode == 124,
                )
                return result.returncode, safe_stdout, safe_stderr
        process = await spawn(
            [str(self._kubectl_path), *command[1:]],
            env=self._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.communicate()
            self._record_evidence(command, 124, "kubectl command timed out", timed_out=True)
            return 124, "", "kubectl command timed out"
        safe_stdout = _redact_output(stdout)
        safe_stderr = _redact_output(stderr)
        self._record_evidence(command, process.returncode, f"{safe_stdout}\n{safe_stderr}")
        return process.returncode, safe_stdout, safe_stderr

    def take_evidence_refs(self) -> list[str]:
        """Return and clear structured evidence refs without exposing command output."""
        refs = [f"command-evidence://{stable_hash(evidence.__dict__)}" for evidence in self._evidence]
        self._evidence.clear()
        return refs

    async def create_model_secret(self, namespace: str, token_file: Path) -> None:
        expected_token_file = Path.home() / ".cache" / "huggingface" / "token"
        if token_file != expected_token_file or not token_file.is_file() or not self.allows_namespace(namespace):
            raise RuntimeConfigurationError("model secret provisioning is outside the configured allowlist")
        create = await spawn(
            [
                str(self._kubectl_path),
                "create",
                "secret",
                "generic",
                "llm-d-hf-token",
                "--namespace",
                namespace,
                f"--from-file=HF_TOKEN={token_file}",
                "--dry-run=client",
                "--output=yaml",
            ],
            env=self._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        secret_yaml, stderr = await create.communicate()
        if create.returncode != 0:
            self._record_evidence(["kubectl", "create", "secret"], create.returncode, _redact_output(stderr))
            raise RuntimeError("host Hugging Face token could not be converted to a Kubernetes Secret")
        apply = await spawn(
            [str(self._kubectl_path), "apply", "--namespace", namespace, "--filename=-"],
            env=self._environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await apply.communicate(secret_yaml)
        self._record_evidence(
            ["kubectl", "apply", "secret"],
            apply.returncode,
            f"{_redact_output(stdout)}\n{_redact_output(stderr)}",
        )
        if apply.returncode != 0:
            raise RuntimeError("host Hugging Face token could not be applied as a Kubernetes Secret")

    async def create_model_secret_from_token(self, namespace: str, token: str) -> None:
        if not self.allows_namespace(namespace) or not token.strip():
            raise RuntimeConfigurationError("model secret provisioning is outside the configured allowlist")
        secret = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "llm-d-hf-token", "namespace": namespace},
                "type": "Opaque",
                "stringData": {"HF_TOKEN": token.strip()},
            }
        ).encode()
        apply = await spawn(
            [str(self._kubectl_path), "apply", "--namespace", namespace, "--filename=-"],
            env=self._environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await apply.communicate(secret)
        self._record_evidence(
            ["kubectl", "apply", "secret"],
            apply.returncode,
            f"{_redact_output(stdout)}\n{_redact_output(stderr)}",
        )
        if apply.returncode != 0:
            raise RuntimeError("supplied Hugging Face token could not be applied as a Kubernetes Secret")

    async def copy_model_secret(self, namespace: str, source_namespace: str, source_name: str) -> None:
        if not self.allows_namespace(namespace) or not all(
            re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", value) for value in (source_namespace, source_name)
        ):
            raise RuntimeConfigurationError("model secret source is outside the configured allowlist")
        read = await spawn(
            [str(self._kubectl_path), "get", "secret", source_name, "--namespace", source_namespace, "--output=json"],
            env=self._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await read.communicate()
        if read.returncode != 0:
            self._record_evidence(["kubectl", "get", "secret"], read.returncode, _redact_output(stderr))
            raise RuntimeError(f"model Secret {source_namespace}/{source_name} is not accessible")
        try:
            token = json.loads(stdout).get("data", {}).get("HF_TOKEN")
        except (AttributeError, json.JSONDecodeError) as error:
            raise RuntimeError("model Secret response is invalid") from error
        if not isinstance(token, str) or not token:
            raise RuntimeError(f"model Secret {source_namespace}/{source_name} does not contain HF_TOKEN")
        secret = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "llm-d-hf-token", "namespace": namespace},
                "type": "Opaque",
                "data": {"HF_TOKEN": token},
            }
        ).encode()
        apply = await spawn(
            [str(self._kubectl_path), "apply", "--namespace", namespace, "--filename=-"],
            env=self._environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        apply_stdout, apply_stderr = await apply.communicate(secret)
        self._record_evidence(
            ["kubectl", "apply", "secret"],
            apply.returncode,
            f"{_redact_output(apply_stdout)}\n{_redact_output(apply_stderr)}",
        )
        if apply.returncode != 0:
            raise RuntimeError("existing model Secret could not be copied to the deployment namespace")

    def _validate(self, command: list[str]) -> None:
        has_kubeconfig = any("=" in item and item.startswith("--kubeconfig") for item in command)
        if not command or command[0] != "kubectl" or has_kubeconfig:
            raise RuntimeConfigurationError("unsupported Kubernetes command")
        namespace_operation = tuple(command[:3])
        is_namespace_command = (
            namespace_operation
            in {
                ("kubectl", "create", "namespace"),
                ("kubectl", "delete", "namespace"),
            }
            and len(command) == 4
        )
        is_async_namespace_delete = namespace_operation == ("kubectl", "delete", "namespace") and command[4:] in (
            ["--wait=false"],
            ["--wait=false", "--ignore-not-found=true"],
            ["--ignore-not-found=true"],
            ["--wait=true", "--ignore-not-found=true"],
            ["--wait=true"],
        )
        is_namespace_command = is_namespace_command or is_async_namespace_delete
        namespace = command[3] if is_namespace_command else _argument_after(command, "--namespace")
        if namespace is None or not self.allows_namespace(namespace) or namespace == self._namespace_prefix:
            raise RuntimeConfigurationError("Kubernetes command namespace is outside the configured allowlist")
        diagnostics_commands = {
            ("kubectl", "get", "pods", "--namespace", namespace, "-o", "wide"),
            ("kubectl", "get", "events", "--namespace", namespace, "--sort-by=.lastTimestamp"),
            (
                "kubectl",
                "get",
                "resourceclaims.resource.k8s.io",
                "--namespace",
                namespace,
                "-o",
                "wide",
            ),
            (
                "kubectl",
                "logs",
                "--namespace",
                namespace,
                "-l",
                "llm-d.ai/role=decode",
                "-c",
                "modelserver",
                "--tail=120",
            ),
            (
                "kubectl",
                "logs",
                "--namespace",
                namespace,
                "-l",
                "llm-d.ai/role=decode",
                "-c",
                "modelserver",
                "--tail=120",
                "--prefix=true",
            ),
            (
                "kubectl",
                "logs",
                "--namespace",
                namespace,
                "-l",
                "llm-d.ai/role=prefill",
                "-c",
                "modelserver",
                "--tail=120",
                "--prefix=true",
            ),
            (
                "kubectl",
                "logs",
                "--namespace",
                namespace,
                "-l",
                "llm-d.ai/role=decode",
                "-c",
                "routing-proxy",
                "--tail=120",
                "--prefix=true",
            ),
            (
                "kubectl",
                "logs",
                "--namespace",
                namespace,
                "-l",
                "llm-d.ai/role=decode",
                "-c",
                "routing-proxy",
                "--previous",
                "--tail=120",
                "--prefix=true",
            ),
        }
        if tuple(command) in diagnostics_commands:
            return
        is_modelserver_pod_lookup = (
            command[:5] == ["kubectl", "get", "pods", "--namespace", namespace]
            and command[5:7] == ["-l", "llm-d.ai/role=decode"]
            and command[7:] == ["-o", "jsonpath={.items[*].metadata.name}"]
        )
        if is_modelserver_pod_lookup:
            return
        is_pod_modelserver_logs = command[:4] == ["kubectl", "logs", "--namespace", namespace] and command[5:] in (
            ["-c", "modelserver", "--tail=120"],
            ["-c", "modelserver", "--previous", "--tail=120"],
        )
        if is_pod_modelserver_logs:
            return
        allowed = {
            ("apply", "--namespace"),
            ("get", "service"),
            ("rollout", "status"),
            ("rollout", "undo"),
            ("set", "env"),
            ("create", "namespace"),
            ("delete", "namespace"),
        }
        is_deployment_scale = len(command) > 2 and command[1] == "scale" and command[2].startswith("deployment/")
        if tuple(command[1:3]) not in allowed and not is_deployment_scale:
            raise RuntimeConfigurationError("Kubernetes command operation is outside the configured allowlist")

    def _record_evidence(self, command: list[str], status: int, output: str, *, timed_out: bool = False) -> None:
        is_namespace_command = tuple(command[:3]) in {
            ("kubectl", "create", "namespace"),
            ("kubectl", "delete", "namespace"),
        }
        namespace = command[3] if is_namespace_command else _argument_after(command, "--namespace")
        self._evidence.append(
            CommandEvidence(
                operation=" ".join(command[1:3]),
                namespace=namespace or "",
                status=status,
                timed_out=timed_out,
                output_checksum=stable_hash({"output": output}),
            )
        )


class RegisteredGuideCommandRunner:
    """Execute only the registered optimized-baseline Helm/Kustomize command plan."""

    def __init__(
        self,
        kubectl_path: Path,
        helm_path: Path,
        namespace_prefix: str,
        guide: RegisteredGuideRuntime,
        timeout_seconds: int,
        rendered_overlay_root: Path,
        kubeconfig: str | None = None,
    ) -> None:
        self._kubectl = RestrictedKubectlRunner(kubectl_path, namespace_prefix, timeout_seconds, kubeconfig)
        self._helm_path = helm_path
        self._namespace_prefix = namespace_prefix
        self._guide = guide
        self._timeout_seconds = timeout_seconds
        self._rendered_overlay_root = rendered_overlay_root.resolve()
        self._evidence: list[CommandEvidence] = []
        self._published_deployments: dict[str, list[str]] = {}

    def register_restored_namespace(self, namespace: str) -> None:
        self._kubectl.register_restored_namespace(namespace)

    def register_published_deployments(self, namespace: str, names: list[str]) -> None:
        if (
            not self._kubectl.allows_namespace(namespace)
            or not names
            or any(
                not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name) for name in names
            )
        ):
            raise RuntimeConfigurationError("published deployments are outside the configured allowlist")
        self._published_deployments[namespace] = list(names)

    async def __call__(self, command: list[str]) -> tuple[int, str, str]:
        if command and command[0] == "kubectl":
            self._validate_kustomize(command)
            result = await self._kubectl(command)
            self._evidence.extend(self._take_kubectl_evidence())
            return result
        self._validate_helm(command)
        process = await spawn(
            [str(self._helm_path), *command[1:]],
            env=self._kubectl._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.communicate()
            self._record(command, 124, "helm command timed out", timed_out=True)
            return 124, "", "helm command timed out"
        safe_stdout, safe_stderr = _redact_output(stdout), _redact_output(stderr)
        self._record(command, process.returncode, f"{safe_stdout}\n{safe_stderr}")
        return process.returncode, safe_stdout, safe_stderr

    def take_evidence_refs(self) -> list[str]:
        refs = [f"command-evidence://{stable_hash(item.__dict__)}" for item in self._evidence]
        self._evidence.clear()
        return refs

    async def create_model_secret(self, namespace: str, token_file: Path) -> None:
        expected_token_file = Path.home() / ".cache" / "huggingface" / "token"
        is_allowed = (
            token_file == expected_token_file and token_file.is_file() and namespace.startswith(self._namespace_prefix)
        )
        if not is_allowed:
            raise RuntimeConfigurationError("model secret provisioning is outside the registered Guide plan")
        create = await spawn(
            [
                str(self._kubectl._kubectl_path),
                "create",
                "secret",
                "generic",
                "llm-d-hf-token",
                "--namespace",
                namespace,
                f"--from-file=HF_TOKEN={token_file}",
                "--dry-run=client",
                "--output=yaml",
            ],
            env=self._kubectl._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        secret_yaml, stderr = await create.communicate()
        if create.returncode != 0:
            self._record(["kubectl", "create", "secret"], create.returncode, _redact_output(stderr))
            raise RuntimeError("host Hugging Face token could not be converted to a Kubernetes Secret")
        apply = await spawn(
            [
                str(self._kubectl._kubectl_path),
                "apply",
                "--namespace",
                namespace,
                "--filename=-",
            ],
            env=self._kubectl._environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await apply.communicate(secret_yaml)
        self._record(
            ["kubectl", "apply", "secret"],
            apply.returncode,
            f"{_redact_output(stdout)}\n{_redact_output(stderr)}",
        )
        if apply.returncode != 0:
            raise RuntimeError("host Hugging Face token could not be applied as a Kubernetes Secret")

    async def create_model_secret_from_token(self, namespace: str, token: str) -> None:
        await self._kubectl.create_model_secret_from_token(namespace, token)
        self._evidence.extend(self._take_kubectl_evidence())

    async def copy_model_secret(self, namespace: str, source_namespace: str, source_name: str) -> None:
        await self._kubectl.copy_model_secret(namespace, source_namespace, source_name)
        self._evidence.extend(self._take_kubectl_evidence())

    async def endpoint_smoke_test(self, namespace: str, endpoint_url: str) -> str | None:
        epp_host = f"{self._guide.endpoint_service_name}.{namespace}.svc"
        baseline_host = f"{self._guide.baseline_service_name}.{namespace}.svc"
        parsed = urlparse(endpoint_url)
        host = parsed.hostname or ""
        if host.rstrip(".") not in {
            epp_host,
            f"{epp_host}.cluster.local",
            baseline_host,
            f"{baseline_host}.cluster.local",
        }:
            raise RuntimeConfigurationError("endpoint smoke test is outside the registered Guide plan")
        command = [
            "kubectl",
            "exec",
            f"deployment/{self._published_deployments.get(namespace, [self._guide.readiness_deployment_name])[0]}",
            "--namespace",
            namespace,
            "--container",
            "modelserver",
            "--",
            "python",
            "-c",
            (
                "from urllib.request import ProxyHandler, build_opener; "
                f"response = build_opener(ProxyHandler({{}})).open({endpoint_url!r} + '/v1/models', timeout=15); "
                "assert response.status == 200"
            ),
        ]
        process = await spawn(
            [str(self._kubectl._kubectl_path), *command[1:]],
            env=self._kubectl._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.communicate()
            self._record(["kubectl", "exec", "endpoint-smoke"], 124, "endpoint smoke test timed out", timed_out=True)
            return "endpoint smoke test timed out"
        output = f"{_redact_output(stdout)}\n{_redact_output(stderr)}"
        self._record(["kubectl", "exec", "endpoint-smoke"], process.returncode, output)
        return None if process.returncode == 0 else "endpoint smoke test failed"

    def _validate_kustomize(self, command: list[str]) -> None:
        namespace = (
            command[3]
            if tuple(command[:3])
            in {
                ("kubectl", "create", "namespace"),
                ("kubectl", "delete", "namespace"),
            }
            else _argument_after(command, "--namespace")
        )
        if namespace is None or not self._kubectl.allows_namespace(namespace):
            raise RuntimeConfigurationError("Kubernetes command namespace is outside the configured allowlist")
        rollout = (
            "kubectl",
            "rollout",
            "status",
            f"deployment/{self._guide.readiness_deployment_name}",
            "--namespace",
            namespace,
        )
        rendered_overlay = _argument_after(command, "--kustomize")
        allowed = {
            rollout,
            ("kubectl", "create", "namespace", namespace),
            ("kubectl", "delete", "namespace", namespace),
            ("kubectl", "delete", "namespace", namespace, "--ignore-not-found=true"),
            ("kubectl", "delete", "namespace", namespace, "--wait=false"),
            (
                "kubectl",
                "delete",
                "namespace",
                namespace,
                "--wait=false",
                "--ignore-not-found=true",
            ),
            (
                "kubectl",
                "delete",
                "namespace",
                namespace,
                "--wait=true",
                "--ignore-not-found=true",
            ),
            (
                "kubectl",
                "scale",
                f"deployment/{self._guide.readiness_deployment_name}",
                "--namespace",
                namespace,
                "--replicas=0",
            ),
            ("kubectl", "get", "pods", "--namespace", namespace, "-o", "wide"),
            ("kubectl", "get", "events", "--namespace", namespace, "--sort-by=.lastTimestamp"),
            (
                "kubectl",
                "logs",
                "--namespace",
                namespace,
                "-l",
                "llm-d.ai/role=decode",
                "-c",
                "modelserver",
                "--tail=120",
            ),
            (
                "kubectl",
                "exec",
                f"deployment/{self._guide.readiness_deployment_name}",
                "--namespace",
                namespace,
                "--container",
                "modelserver",
            ),
        }
        for name in self._published_deployments.get(namespace, []):
            allowed.add(("kubectl", "rollout", "status", f"deployment/{name}", "--namespace", namespace))
            allowed.add(("kubectl", "scale", f"deployment/{name}", "--namespace", namespace, "--replicas=0"))
        if rendered_overlay is not None:
            path = Path(rendered_overlay).resolve()
            if path.is_dir() and path.is_relative_to(self._rendered_overlay_root):
                allowed.add(("kubectl", "apply", "--namespace", namespace, "--kustomize", str(path)))
        prefix = tuple(command[: len(command) - 1]) if command[-1].startswith("--timeout=") else tuple(command)
        is_endpoint_smoke = tuple(command[:7]) == (
            "kubectl",
            "exec",
            f"deployment/{self._guide.readiness_deployment_name}",
            "--namespace",
            namespace,
            "--container",
            "modelserver",
        ) and command[7:9] == ["--", "python"]
        if prefix not in allowed and not is_endpoint_smoke:
            raise RuntimeConfigurationError("Kubernetes command operation is outside the registered Guide plan")

    def _validate_helm(self, command: list[str]) -> None:
        namespace = _argument_after(command, "--namespace")
        if namespace is None or not self._kubectl.allows_namespace(namespace):
            raise RuntimeConfigurationError("Helm command namespace is outside the configured allowlist")
        install = [
            "helm",
            "upgrade",
            "--install",
            self._guide.router_release_name or "",
            self._guide.router_chart or "",
            "--namespace",
            namespace,
            "--version",
            self._guide.router_chart_version or "",
            "--values",
            str(self._guide.router_base_values_path),
            "--values",
            str(self._guide.router_values_path),
        ]
        neutral_install = [
            "helm",
            "upgrade",
            "--install",
            self._guide.router_release_name or "",
            self._guide.router_chart or "",
            "--namespace",
            namespace,
            "--version",
            self._guide.router_chart_version or "",
            "--values",
            str(self._guide.router_base_values_path),
            "--values",
            str(self._guide.neutral_router_values_path),
        ]
        uninstall = ["helm", "uninstall", self._guide.router_release_name or "", "--namespace", namespace]
        if command not in (install, neutral_install, uninstall):
            raise RuntimeConfigurationError("Helm command operation is outside the registered Guide plan")

    def _take_kubectl_evidence(self) -> list[CommandEvidence]:
        refs = self._kubectl.take_evidence_refs()
        return [CommandEvidence("kubectl", "", 0, False, stable_hash({"ref": ref})) for ref in refs]

    def _record(self, command: list[str], status: int, output: str, *, timed_out: bool = False) -> None:
        namespace = _argument_after(command, "--namespace") or ""
        self._evidence.append(
            CommandEvidence(" ".join(command[1:3]), namespace, status, timed_out, stable_hash({"output": output}))
        )


def build_deployment_service_from_environment(
    environ: Mapping[str, str] | None = None,
) -> tuple[GuideAdapterDeploymentService, GuideCatalog] | None:
    """Build the Configuration-driven Deploy runtime from approved environment ports."""
    settings = RuntimeEnvironment.from_environment(environ)
    if settings is None:
        return None

    providers = [
        _build_optimized_baseline_provider(settings),
        _build_baseline_vllm_provider(settings),
        _build_pd_disaggregation_provider(settings),
        _build_tiered_prefix_cache_provider(settings),
        _build_precise_prefix_cache_routing_provider(settings),
    ]
    catalog = GuideCatalog(
        [
            ConfigurationManifestAdapter(
                provider,
                RestrictedKubectlRunner(
                    settings.kubectl_path,
                    settings.namespace_prefix,
                    max(settings.command_timeout_seconds, settings.readiness_timeout_seconds),
                    settings.kubeconfig,
                ),
                settings.namespace_prefix,
                settings.readiness_timeout_seconds,
            )
            for provider in providers
        ]
    )
    return GuideAdapterDeploymentService(catalog, build_namespace_factory(settings.namespace_prefix)), catalog


def _build_optimized_baseline_provider(settings: RuntimeEnvironment) -> OptimizedBaselineGuideAdapter:
    if settings.helm_path is None:
        raise RuntimeConfigurationError("LLM_D_BENCH_HELM_PATH is required for the optimized-baseline provider")
    guide_root = settings.manifest_root / "llm-d"
    if not guide_root.is_dir():
        guide_root = settings.manifest_root
    overlay_path = guide_root / "guides" / "optimized-baseline" / "modelserver" / overlay_variant(accelerator=settings.accelerator) / "vllm"
    router_base_values_path = guide_root / "guides" / "recipes" / "router" / "base.values.yaml"
    router_values_path = guide_root / "guides" / "optimized-baseline" / "router" / "optimized-baseline.values.yaml"
    neutral_router_values_path = (
        Path(__file__).resolve().parents[1] / "providers" / "router_values" / "router-neutral.values.yaml"
    )
    gateway_mode_values_path = (
        Path(__file__).resolve().parents[1] / "providers" / "router_values" / "router-gateway-mode.values.yaml"
    )
    if not all(
        (
            overlay_path.is_dir(),
            router_base_values_path.is_file(),
            router_values_path.is_file(),
            neutral_router_values_path.is_file(),
            gateway_mode_values_path.is_file(),
        )
    ):
        raise RuntimeConfigurationError(
            "optimized-baseline provider artifacts are missing from the configured source tree"
        )
    definition = GuideDefinition(
        guide_id="optimized-baseline",
        source_ref="local-optimized-baseline",
        content_hash=stable_hash(_tree_checksum(overlay_path)),
        maturity="supported-core",
        capabilities={
        "variant": f"{overlay_variant(accelerator=settings.accelerator)}-routed-guide",
            "endpoint_service_name": "optimized-baseline-epp",
            "endpoint_service_port": 80,
        },
    )
    # Preserve upstream relative resource paths in a disposable copy of the Guide
    # tree; generated overlays must never mutate the source checkout.
    import shutil

    scratch_guide = storage_path("scratch", "deployment-overlays", stable_hash(str(guide_root)))
    shutil.copytree(guide_root / "guides", scratch_guide / "guides", dirs_exist_ok=True)
    rendered_overlay_root = scratch_guide / overlay_path.parent.relative_to(guide_root)
    guide = RegisteredGuideRuntime(
        guide_id=definition.guide_id,
        source_ref=definition.source_ref,
        content_hash=definition.content_hash,
        maturity=definition.maturity,
        manifest_path=overlay_path,
        adapter_type=f"optimized-baseline-{overlay_variant(accelerator=settings.accelerator)}",
        guide_root=guide_root,
        router_base_values_path=router_base_values_path,
        router_values_path=router_values_path,
        neutral_router_values_path=neutral_router_values_path,
        router_chart="oci://ghcr.io/llm-d/charts/llm-d-router-standalone",
        router_chart_version=router_chart_version(),
        router_release_name="optimized-baseline",
        readiness_deployment_name=f"optimized-baseline-{overlay_variant(accelerator=settings.accelerator)}-vllm-decode",
        endpoint_service_name="optimized-baseline-epp",
        endpoint_service_port=80,
        baseline_service_name="optimized-baseline-modelserver",
        baseline_service_port=8000,
        proxy_environment=settings.model_environment,
    )
    command_runner = RegisteredGuideCommandRunner(
        settings.kubectl_path,
        settings.helm_path,
        settings.namespace_prefix,
        guide,
        max(settings.command_timeout_seconds, settings.readiness_timeout_seconds),
        rendered_overlay_root,
        settings.kubeconfig,
    )
    return OptimizedBaselineGuideAdapter(
        definition,
        command_runner,
        policy=OptimizedBaselineGuidePolicy(
            namespace_prefix=settings.namespace_prefix,
            guide_root=guide_root,
            overlay_path=overlay_path,
            router_base_values_path=router_base_values_path,
            router_values_path=router_values_path,
            neutral_router_values_path=neutral_router_values_path,
            gateway_mode_values_path=gateway_mode_values_path,
            router_chart=guide.router_chart or "",
            router_chart_version=guide.router_chart_version or "",
            router_release_name=guide.router_release_name or "",
            model_deployment_name=guide.readiness_deployment_name or "",
            endpoint_service_name=guide.endpoint_service_name or "",
            endpoint_service_port=guide.endpoint_service_port or 0,
            baseline_service_name="optimized-baseline-modelserver",
            baseline_service_port=8000,
            model_token_file=Path.home() / ".cache" / "huggingface" / "token",
            rendered_overlay_root=rendered_overlay_root,
            proxy_environment=guide.proxy_environment or {},
            retain_namespace_on_failure=settings.retain_namespace_on_failure,
            readiness_timeout_seconds=settings.readiness_timeout_seconds,
            docker_path=settings.docker_path,
        ),
    )


def _build_baseline_vllm_provider(settings: RuntimeEnvironment) -> BaselineVllmAdapter:
    runner = RestrictedKubectlRunner(
        settings.kubectl_path,
        settings.namespace_prefix,
        max(settings.command_timeout_seconds, settings.readiness_timeout_seconds),
        settings.kubeconfig,
    )
    return BaselineVllmAdapter(
        runner, settings.namespace_prefix, settings.readiness_timeout_seconds, settings.docker_path
    )


def _build_pd_disaggregation_provider(settings: RuntimeEnvironment) -> PdDisaggregationAdapter:
    if settings.helm_path is None:
        raise RuntimeConfigurationError("LLM_D_BENCH_HELM_PATH is required for the pd-disaggregation provider")
    runner = RestrictedKubectlRunner(
        settings.kubectl_path,
        settings.namespace_prefix,
        max(settings.command_timeout_seconds, settings.readiness_timeout_seconds),
        settings.kubeconfig,
    )
    guide_root = settings.manifest_root / "llm-d"
    if not guide_root.is_dir():
        guide_root = settings.manifest_root
    return PdDisaggregationAdapter(
        runner,
        guide_root,
        settings.namespace_prefix,
        settings.readiness_timeout_seconds,
        settings.helm_path,
        settings.kubeconfig,
    )


def _build_tiered_prefix_cache_provider(settings: RuntimeEnvironment) -> HelmKustomizeGuideAdapter:
    if settings.helm_path is None:
        raise RuntimeConfigurationError("LLM_D_BENCH_HELM_PATH is required for tiered-prefix-cache")
    guide_root = settings.manifest_root / "llm-d"
    if not guide_root.is_dir():
        guide_root = settings.manifest_root
    descriptor = HelmKustomizeGuideDescriptor(
        guide_id="tiered-prefix-cache",
        guide_root=guide_root,
        variants={
            "base": guide_root / f"guides/tiered-prefix-cache/modelserver/{overlay_variant(accelerator=settings.accelerator)}/vllm/base",
            "native/cpu/base": guide_root / f"guides/tiered-prefix-cache/modelserver/{overlay_variant(accelerator=settings.accelerator)}/vllm/native/cpu/base",
            "lmcache-connector/cpu/base": guide_root
            / f"guides/tiered-prefix-cache/modelserver/{overlay_variant(accelerator=settings.accelerator)}/vllm/lmcache-connector/cpu/base",
        },
        default_variant="native/cpu/base",
        router_chart="oci://ghcr.io/llm-d/charts/llm-d-router-standalone",
        router_version=router_chart_version(),
        router_values=(
            guide_root / "guides/recipes/router/base.values.yaml",
            guide_root / "guides/tiered-prefix-cache/router/tiered-prefix-cache-cpu.values.yaml",
        ),
        release_name="tiered-prefix-cache",
        readiness_deployments=(f"{overlay_variant(accelerator=settings.accelerator)}-vllm-decode",),
        endpoint_service="tiered-prefix-cache-epp",
        endpoint_port=80,
        data_plane_kind="llm-d-router",
    )
    runner = RestrictedKubectlRunner(
        settings.kubectl_path,
        settings.namespace_prefix,
        max(settings.command_timeout_seconds, settings.readiness_timeout_seconds),
        settings.kubeconfig,
    )
    return HelmKustomizeGuideAdapter(
        descriptor,
        runner,
        settings.kubectl_path,
        settings.helm_path,
        settings.namespace_prefix,
        settings.readiness_timeout_seconds,
        settings.kubeconfig,
        accelerator=settings.accelerator,
    )


def _build_precise_prefix_cache_routing_provider(settings: RuntimeEnvironment) -> PrecisePrefixCacheRoutingAdapter:
    if settings.helm_path is None:
        raise RuntimeConfigurationError("LLM_D_BENCH_HELM_PATH is required for precise-prefix-cache-routing")
    guide_root = settings.manifest_root / "llm-d"
    if not guide_root.is_dir():
        guide_root = settings.manifest_root
    runner = RestrictedKubectlRunner(
        settings.kubectl_path,
        settings.namespace_prefix,
        max(settings.command_timeout_seconds, settings.readiness_timeout_seconds),
        settings.kubeconfig,
    )
    return PrecisePrefixCacheRoutingAdapter(
        runner,
        guide_root,
        settings.namespace_prefix,
        settings.readiness_timeout_seconds,
        settings.helm_path,
        settings.kubeconfig,
        accelerator=settings.accelerator,
    )


def build_namespace_factory(namespace_prefix: str):
    """Build DNS-safe namespaces unique to one Guide deployment request."""

    def namespace_factory(request) -> str:
        policy = request.deployment_policy.value.get("namespace_policy") or {}
        requested_prefix = policy.get("prefix") if isinstance(policy, dict) else None
        artifact = request.configuration_artifacts[0]
        content = json.loads(artifact.content or "{}")
        decode = content.get("decode") if isinstance(content, dict) else None
        prefill = content.get("prefill") if isinstance(content, dict) else None
        decode = decode if isinstance(decode, dict) else {}
        prefill = prefill if isinstance(prefill, dict) else {}
        replicas = int(decode.get("replicaCount", prefill.get("replicaCount", 1)))
        tensor_parallelism = int(decode.get("tensorParallelSize", prefill.get("tensorParallelSize", 1)))
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        if requested_prefix is not None:
            if not isinstance(requested_prefix, str) or not re.fullmatch(
                r"[a-z0-9]+(?:-[a-z0-9]+)*-", requested_prefix
            ):
                raise RuntimeConfigurationError("deployment namespace policy prefix is invalid")
            suffix = f"{replicas}-{tensor_parallelism}-{timestamp}"
            if len(requested_prefix) + len(suffix) > 63:
                raise RuntimeConfigurationError(
                    "deployment namespace policy prefix leaves no space for the namespace suffix"
                )
            return f"{requested_prefix}{suffix}"
        guide_id = str(request.provenance["guide_id"])
        suffix = f"-{replicas}-{tensor_parallelism}-{timestamp}"
        available_guide_length = 63 - len(namespace_prefix) - len(suffix)
        normalized_guide_id = re.sub(r"[^a-z0-9-]+", "-", guide_id.lower()).strip("-")
        if not normalized_guide_id or available_guide_length < 1:
            raise RuntimeConfigurationError("namespace prefix leaves no space for a registered Guide identifier")
        return f"{namespace_prefix}{normalized_guide_id[:available_guide_length].rstrip('-')}{suffix}"

    return namespace_factory


def _positive_integer(values: Mapping[str, str], name: str, default: int) -> int:
    value = values.get(name, str(default))
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeConfigurationError(f"{name} must be a positive integer") from error
    if parsed <= 0:
        raise RuntimeConfigurationError(f"{name} must be a positive integer")
    return parsed


def _model_environment(values: Mapping[str, str]) -> dict[str, str]:
    """Return the small, explicit environment allowlist for deployed model pods."""
    environment = {
        name: value for name in ("HTTP_PROXY", "HTTPS_PROXY", "HF_ENDPOINT") if (value := values.get(name, "").strip())
    }
    if "HTTP_PROXY" in environment or "HTTPS_PROXY" in environment:
        no_proxy_entries = [entry.strip() for entry in values.get("NO_PROXY", "").split(",") if entry.strip()]
        for entry in ("localhost", "127.0.0.1", ".svc", ".cluster.local"):
            if entry not in no_proxy_entries:
                no_proxy_entries.append(entry)
        environment["NO_PROXY"] = ",".join(no_proxy_entries)
    return environment


def _enabled(values: Mapping[str, str], name: str) -> bool:
    return values.get(name, "").lower() in {"1", "true", "yes"}


def _optional_executable_file(values: Mapping[str, str], name: str, command: str) -> Path | None:
    value = values.get(name, "").strip()
    if not value:
        value = shutil.which(command, path=values.get("PATH")) or ""
    if not value:
        return None
    configured_path = Path(value)
    if not configured_path.is_absolute():
        raise RuntimeConfigurationError(f"{name} must be an executable absolute path")
    path = configured_path.resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeConfigurationError(f"{name} must be an executable absolute path")
    return path


def _required_executable_file(values: Mapping[str, str], name: str, command: str) -> Path:
    path = _optional_executable_file(values, name, command)
    if path is None:
        raise RuntimeConfigurationError(f"{name} must be configured or {command} must be available on PATH")
    return path


def _argument_after(command: list[str], argument: str) -> str | None:
    try:
        return command[command.index(argument) + 1]
    except (ValueError, IndexError):
        return None


def _redact_output(value: bytes) -> str:
    text = value.decode(errors="replace")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", text)
    return re.sub(r"(?i)(token|password|secret|authorization)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)


def _tree_checksum(path: Path) -> dict[str, str]:
    return {
        str(item.relative_to(path)): stable_hash({"content": item.read_text(encoding="utf-8")})
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }
