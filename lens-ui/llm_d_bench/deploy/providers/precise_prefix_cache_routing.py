"""Precise prefix-cache routing (KV-event driven, EPP-scored) deployment adapter.

Deploys the upstream Kustomize modelserver overlay together with the upstream
render (tokenizer) Service and a Prism-authored baseline round-robin Service,
then Helm-installs the llm-d router with the Guide's KV-cache-aware EPP plugin
chain patched to match the requested model. On readiness this exposes two
endpoints: the EPP-routed `endpoint_url` and a `baseline_endpoint_url` (a plain
Kubernetes Service, no EPP) -- so Evaluate can compare precise routing against
a stock Service without a second deployment, matching the Guide's own
published benchmark methodology (see upstream
`guides/precise-prefix-cache-routing/benchmark-results/*.md`).

Modeled on `pd_disaggregation.py` (list-style container `args`, not the
shell-string convention `helm_kustomize.py` assumes) rather than reusing
`HelmKustomizeGuideAdapter`, since this Guide's upstream template uses a
different container-args shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from llm_d_bench.common.hashing import stable_hash
from llm_d_bench.configuration.manifest_facts import _invocation_tokens, _model_argument, _option
from llm_d_bench.deploy.data_plane import router_data_plane_args, router_data_plane_effective_values
from llm_d_bench.deploy.providers.deployment_bundle import (
    install_deployment_bundle,
    subprocess_bundle_runner,
    text_checksum,
)
from llm_d_bench.deploy.providers.guide_adapter import GuideDefinition, GuideDeploymentArtifact, ValidationResult
from llm_d_bench.deploy.providers.hardware_profile import (
    overlay_variant,
    requires_dra_claim,
    set_accelerator_request,
)
from llm_d_bench.deploy.providers.model_cache_environment import model_cache_environment
from llm_d_bench.versions import router_chart_version

# Upstream sample calibration is used only during the short bootstrap interval
# before the model is ready. Readiness always replaces it with a live measurement
# from the selected model/TP/replica/hardware deployment before Evaluate can run.
UPSTREAM_SAMPLE_PEAK_PREFILL_THROUGHPUT = 15926

_ROUTER_CHART = "oci://ghcr.io/llm-d/charts/llm-d-router-standalone"
_ROUTER_CHART_VERSION = router_chart_version()
_RELEASE_NAME = "precise-prefix-cache-routing"

# Grace period for the EPP to finish registering a healthy decode backend
# after its calibration-triggered restart, before Evaluate's warmup can race
# it with "503 no healthy upstream". An active probe is not allowlist-safe
# (see readiness()); this is a time-bounded mitigation instead.
_EPP_ENDPOINT_SETTLE_SECONDS = 20


def _modelserver_invocations(manifest_text: str):
    """Yield precise-provider invocations lazily in manifest order."""
    for document in yaml.safe_load_all(manifest_text):
        if not isinstance(document, dict) or document.get("kind") != "Deployment":
            continue
        deployment_name = str((document.get("metadata") or {}).get("name") or "precise")
        containers = document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            if container.get("name") != "modelserver":
                continue
            tokens = _invocation_tokens(container, deployment_name)
            yield deployment_name, tokens


def _parse_peak_prefill_throughput(output: str) -> int | None:
    """Extract the measured value from the upstream calibration recipe output.

    On success the recipe prints ``Measured peakPrefillThroughput = <n>
    tokens/sec``; the Job's own ``PEAK_PREFILL_THROUGHPUT=<n>`` line may also be
    echoed on failure. Return the last positive value, or None.
    """
    matches = re.findall(
        r"(?m)(?:^PEAK_PREFILL_THROUGHPUT=|Measured peakPrefillThroughput\s*=\s*)(\d+)", output
    )
    for value in reversed(matches):
        parsed = int(value)
        if parsed > 0:
            return parsed
    return None


class PrecisePrefixCacheRoutingAdapter:
    """Structured XPU precise-prefix-cache-routing deployment adapter."""

    published_manifest_lifecycle = True

    def __init__(
        self,
        command_runner,
        guide_root: Path,
        namespace_prefix: str,
        timeout: int,
        helm_path: Path,
        kubeconfig: str | None,
        accelerator: str | None = None,
    ) -> None:
        self._runner = command_runner
        # Resolved once per adapter instance (one per deployment run) from the
        # run's own accelerator, never a shared global: concurrent runs for
        # different clusters/vendors must not race on a single overlay choice.
        self._accelerator = accelerator
        self._overlay_variant_value = overlay_variant(accelerator=accelerator)
        self._modelserver_source = self._modelserver_overlay(guide_root)
        self._render_source = guide_root / "guides/precise-prefix-cache-routing/render"
        self._baseline_source = (
            Path(__file__).resolve().parent / "guide_overlays" / "precise-prefix-cache-routing" / "baseline"
        )
        self._router_base_values = guide_root / "guides/recipes/router/base.values.yaml"
        self._router_values = (
            guide_root / "guides/precise-prefix-cache-routing/router/precise-prefix-cache-routing.values.yaml"
        )
        self._calibration_script = guide_root / "guides/recipes/router/calibration/calibrate.sh"
        self._root = Path(tempfile.gettempdir()) / "prism-precise-prefix-cache-routing"
        self._namespace_prefix = namespace_prefix
        self._timeout = timeout
        self._helm_path = helm_path
        self._environment = {**os.environ, "KUBECONFIG": kubeconfig} if kubeconfig else None
        self._guide_root = guide_root
        self._bundle_command_runner = subprocess_bundle_runner(
            helm_path,
            Path(getattr(command_runner, "_kubectl_path", "kubectl")),
            self._environment,
        )
        self._bundle_calibration_scripts: dict[str, Path] = {}
        self._definition = GuideDefinition(
            "precise-prefix-cache-routing",
            "local-precise-prefix-cache-routing",
            stable_hash(
                {
                    "modelserver": str(self._modelserver_source),
                    "render": str(self._render_source),
                    "baseline": str(self._baseline_source),
                    "router_values": str(self._router_values),
                }
            ),
            "supported-extension",
            {
                "variant": f"{self._overlay_variant_value}-routed-guide",
                "structured_custom_parameters": True,
                "endpoint_service_name": f"{_RELEASE_NAME}-epp",
                "endpoint_service_port": 80,
                "baseline_endpoint_service_name": f"{_RELEASE_NAME}-baseline",
                "baseline_endpoint_service_port": 8000,
            },
        )

    def _modelserver_overlay(self, guide_root: Path) -> Path:
        """Model-server Kustomize overlay for this run's resolved hardware.

        Intel XPU keeps its overlay directly under ``modelserver/xpu/vllm``;
        every other profile (notably NVIDIA) uses the generic ``gpu/vllm/base``
        overlay.
        """
        modelserver = guide_root / "guides/precise-prefix-cache-routing/modelserver"
        if self._overlay_variant_value == "xpu":
            return modelserver / "xpu/vllm"
        return modelserver / f"{self._overlay_variant_value}/vllm/base"

    def discover(self) -> GuideDefinition:
        return self._definition

    def validate_inputs(
        self, definition, cluster_snapshot: dict[str, Any], overrides: dict[str, Any]
    ) -> ValidationResult:
        try:
            self._parameters(overrides)
            for path in (self._modelserver_source, self._render_source, self._baseline_source):
                if not path.is_dir():
                    raise ValueError(f"registered precise-prefix-cache-routing artifact is unavailable: {path}")
            if not self._router_base_values.is_file() or not self._router_values.is_file():
                raise ValueError("registered precise-prefix-cache-routing router values are unavailable")
            if not self._calibration_script.is_file():
                raise ValueError("registered router calibration recipe is unavailable")
        except ValueError as error:
            return ValidationResult(False, [str(error)])
        return ValidationResult(True)

    async def render(self, definition, overrides: dict[str, Any]) -> GuideDeploymentArtifact:
        if definition.guide_id != self._definition.guide_id:
            raise ValueError("adapter cannot render a different guide")
        parameters = self._parameters(overrides)
        digest = stable_hash(parameters)
        directory = self._root / digest
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)

        documents = await self._kustomize_build(self._modelserver_source)
        self._patch_modelserver(documents, parameters)
        manifest = directory / "manifest.yaml"
        manifest.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")

        if parameters["router_values_override"]:
            (directory / "router-values-override.yaml").write_text(
                parameters["router_values_override"],
                encoding="utf-8",
            )
        else:
            (directory / "router-values-patched.yaml").write_text(
                self._patched_router_values(parameters["model"], parameters["peak_prefill_throughput"]),
                encoding="utf-8",
            )

        return GuideDeploymentArtifact(
            guide_id=self._definition.guide_id,
            artifact_hash=digest,
            manifest_ref=str(manifest),
            source_ref=self._definition.source_ref,
            guide_content_hash=self._definition.content_hash,
            manifest_checksum=stable_hash({"parameters": parameters}),
            # llm-d router data plane: reached through the shared Gateway unless
            # the deployment is evaluation-owned (then it keeps its own proxy).
            deployment_contract={"data_plane_kind": "llm-d-router"},
        )

    async def deploy(self, artifact: GuideDeploymentArtifact, execution_context: dict[str, Any]) -> dict[str, Any]:
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
                effective_values_content=router_data_plane_effective_values(
                    bundle["helm"]["values"][-1]["content"], execution_context
                ),
                effective_values_name="router-effective-precise-routing.yaml",
            )
            router_values_path = Path(reproducibility["router_values_path"])
            calibration_path = reproducibility.get("calibration_script_path")
            if calibration_path:
                self._bundle_calibration_scripts[namespace] = Path(calibration_path)
        else:
            for source in (self._render_source, self._baseline_source):
                status, stdout, stderr = await self._runner(
                    [
                        "kubectl",
                        "apply",
                        "--namespace",
                        namespace,
                        "-k",
                        str(source),
                    ]
                )
                if status != 0:
                    raise RuntimeError((stderr or stdout).strip())
            router_values_path = self._resolve_router_values(artifact)
            helm = await asyncio.create_subprocess_exec(
                str(self._helm_path),
                "upgrade",
                "--install",
                _RELEASE_NAME,
                _ROUTER_CHART,
                "--namespace",
                namespace,
                "--version",
                _ROUTER_CHART_VERSION,
                "--values",
                str(self._router_base_values),
                "--values",
                str(router_values_path),
                *router_data_plane_args(execution_context),
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
        manifest_text = Path(artifact.manifest_ref or "").read_text(encoding="utf-8")
        return {
            "namespace": namespace,
            "artifact_hash": artifact.artifact_hash,
            "apply_output": stdout,
            "model": self._extract_model_name(manifest_text),
            "calibration_chunk_size": self._extract_calibration_chunk_size(manifest_text),
            "router_values_path": str(router_values_path),
            # Carried so the post-calibration router re-render applies the same
            # data-plane values (plaintext EPP ext_proc, proxy disable).
            "data_plane": execution_context.get("data_plane"),
            "provenance": execution_context.get("provenance"),
            **reproducibility,
        }

    def _decode_deployment(self, artifact: object) -> str:
        """Decode Deployment name as rendered for this run's resolved hardware.

        Names are hardware specific (the NVIDIA overlay renders
        ``precise-prefix-cache-routing-gpu-vllm-*`` while Intel XPU renders
        ``precise-prefix-cache-routing-xpu-vllm-*``), so they are read from the
        deployment contract or the rendered manifest rather than hardcoded.
        """
        contract = artifact.deployment_contract if isinstance(artifact, GuideDeploymentArtifact) else {}
        names = [str(name) for name in contract.get("readinessDeployments") or []]
        decode = next((name for name in names if name.endswith("decode")), None)
        if decode:
            return decode
        manifest_ref = getattr(artifact, "manifest_ref", None)
        if manifest_ref and Path(manifest_ref).is_file():
            for item in yaml.safe_load_all(Path(manifest_ref).read_text(encoding="utf-8")):
                if (
                    isinstance(item, dict)
                    and item.get("kind") == "Deployment"
                    and str((item.get("metadata") or {}).get("name") or "").endswith("decode")
                ):
                    return str(item["metadata"]["name"])
        return f"precise-prefix-cache-routing-{getattr(self, '_overlay_variant_value', None) or overlay_variant()}-vllm-decode"

    async def readiness(self, execution: dict[str, Any]) -> ValidationResult:
        namespace = execution["namespace"]
        calibration_script_path = execution.get("calibration_script_path")
        if calibration_script_path:
            self._bundle_calibration_scripts[namespace] = Path(str(calibration_script_path))
        status, stdout, stderr = await self._runner(
            [
                "kubectl",
                "rollout",
                "status",
                f"deployment/{self._decode_deployment(execution.get('_artifact'))}",
                "--namespace",
                namespace,
                f"--timeout={self._timeout}s",
            ]
        )
        if status != 0:
            return ValidationResult(False, [(stderr or stdout or "deployment_not_ready").strip()])
        measured = execution.get("calibrated_peak_prefill_throughput")
        if not isinstance(measured, int) or measured <= 0:
            try:
                measured = await self._calibrate_peak_prefill_throughput(
                    namespace,
                    str(execution.get("model") or ""),
                    int(execution.get("calibration_chunk_size") or 8192),
                )
                await self._apply_calibrated_router_values(execution, measured)
            except (OSError, RuntimeError, ValueError) as error:
                return ValidationResult(False, [f"peakPrefillThroughput calibration failed: {error}"])
        status, stdout, stderr = await self._runner(
            [
                "kubectl",
                "rollout",
                "status",
                f"deployment/{_RELEASE_NAME}-epp",
                "--namespace",
                namespace,
                f"--timeout={self._timeout}s",
            ]
        )
        if status != 0:
            return ValidationResult(False, [(stderr or stdout or "calibrated_router_not_ready").strip()])
        execution["calibrated_peak_prefill_throughput"] = measured
        execution["endpoint_url"] = f"http://{_RELEASE_NAME}-epp.{namespace}.svc:80"
        execution["baseline_endpoint_url"] = f"http://{_RELEASE_NAME}-baseline.{namespace}.svc:8000"
        # `kubectl rollout status` for the EPP Deployment only reflects its own
        # pod readiness probe; it does not guarantee the EPP has finished
        # registering a healthy decode endpoint in its routing table. The
        # calibration step above always re-applies the router Helm values
        # (even on an unchanged measurement), which restarts the EPP pod, so a
        # request sent immediately after rollout status succeeds can still
        # race that registration and get Envoy's "503 no healthy upstream" --
        # exactly what Evaluate's warmup/first benchmark stage hit. An active
        # HTTP probe would need `kubectl exec` (or an API-server proxy call),
        # which `RestrictedKubectlRunner`'s allowlist deliberately excludes, so
        # this grace period is the allowlist-compliant mitigation; a probe-based
        # replacement needs an explicit allowlist decision first.
        await asyncio.sleep(_EPP_ENDPOINT_SETTLE_SECONDS)
        return ValidationResult(True)

    async def diagnostics(self, execution: dict[str, Any]) -> dict[str, Any]:
        namespace = execution["namespace"]

        async def capture(command: list[str]) -> str:
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
                    "llm-d.ai/role=decode",
                    "-c",
                    "modelserver",
                    "--tail=120",
                    "--prefix=true",
                ]
            ),
            "calibrated_peak_prefill_throughput": execution.get("calibrated_peak_prefill_throughput"),
            "calibration_output": execution.get("calibration_output"),
            **{
                key: value
                for key, value in execution.items()
                if key == "model" or key.startswith("router_") or key.startswith("calibration_")
            },
        }

    def _ensure_calibration_job_template(self, script_path: Path) -> None:
        """Ensure the recipe's Job template sits beside the script it reads.

        ``calibrate.sh`` resolves ``calibration-peak-throughput.yaml`` from its
        own directory. Bundles saved before the template was bundled (and the
        live Guide tree) provide it next to the source recipe; copy it beside a
        materialized script when it is missing.
        """
        template = script_path.parent / "calibration-peak-throughput.yaml"
        if template.is_file():
            return
        source = self._calibration_script.parent / "calibration-peak-throughput.yaml"
        if source.is_file():
            template.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    async def _calibrate_peak_prefill_throughput(self, namespace: str, model: str, chunk_size: int) -> int:
        if not model:
            raise ValueError("model name could not be derived from the deployed modelserver manifest")
        environment = {
            **os.environ,
            **(self._environment or {}),
            # The upstream recipe renders its Job to a fixed
            # /tmp/<GUIDE_NAME>-calibrate-peak-throughput.yaml; a stale file
            # owned by another user blocks that write. A per-deployment name
            # keeps the path unique and writable. VLLM_ENDPOINT is set below, so
            # the recipe never auto-discovers the <GUIDE_NAME>-epp service.
            "GUIDE_NAME": f"{_RELEASE_NAME}-{namespace}",
            "NAMESPACE": namespace,
            # Measure the exact same modelserver pods while bypassing EPP. This avoids
            # using the uncalibrated affinity threshold to calibrate itself.
            "VLLM_ENDPOINT": f"http://{_RELEASE_NAME}-baseline.{namespace}.svc:8000",
            "MODEL_NAME": model,
            "CHUNK_SIZE": str(chunk_size),
        }
        calibration_script = Path(
            getattr(self, "_bundle_calibration_scripts", {}).get(namespace, self._calibration_script)
        )
        self._ensure_calibration_job_template(calibration_script)
        process = await asyncio.create_subprocess_exec(
            "bash",
            str(calibration_script),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(360, self._timeout))
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise RuntimeError("upstream calibration Job timed out") from error
        output = (stdout + b"\n" + stderr).decode(errors="replace")
        if process.returncode:
            raise RuntimeError(output[-4000:].strip() or "upstream calibration recipe failed")
        measured = _parse_peak_prefill_throughput(output)
        if measured is None:
            raise RuntimeError(
                "upstream calibration recipe emitted no valid PEAK_PREFILL_THROUGHPUT value: "
                + output[-1500:].strip()
            )
        # The recipe leaves its Job behind (it only clears the previous one), and
        # its Completed pod would otherwise read as "not ready" in the
        # deployment's pod list. Best-effort cleanup.
        with contextlib.suppress(Exception):
            await self._runner(
                [
                    "kubectl",
                    "delete",
                    "job",
                    "calibrate-peak-throughput",
                    "--namespace",
                    namespace,
                    "--ignore-not-found=true",
                ]
            )
        return measured

    async def _apply_calibrated_router_values(self, execution: dict[str, Any], measured: int) -> None:
        source_path = Path(str(execution.get("router_values_path") or ""))
        if not source_path.is_file():
            raise RuntimeError("deployed router values are unavailable for calibration update")
        calibrated_path = source_path.parent / f"router-values-calibrated-{execution['namespace']}.yaml"
        calibrated_path.write_text(
            self._patched_router_values_content(
                source_path.read_text(encoding="utf-8"),
                str(execution.get("model") or ""),
                measured,
            ),
            encoding="utf-8",
        )
        if execution.get("router_chart"):
            release_name = str(execution["router_release_name"])
            common = [
                release_name,
                str(execution["router_chart"]),
                "--namespace",
                execution["namespace"],
                "--version",
                str(execution["router_version"]),
                "--values",
                str(calibrated_path),
                *router_data_plane_args(execution),
            ]
            status, stdout, stderr = await self._bundle_command_runner(["helm", "template", *common])
            if status != 0:
                raise RuntimeError((stderr or stdout or "calibrated Router Helm render failed").strip())
            status, stdout, stderr = await self._bundle_command_runner(["helm", "upgrade", "--install", *common])
            if status != 0:
                raise RuntimeError((stderr or stdout or "router calibration upgrade failed").strip())
            status, rendered, stderr = await self._bundle_command_runner(
                [
                    "helm",
                    "get",
                    "manifest",
                    release_name,
                    "--namespace",
                    execution["namespace"],
                ]
            )
            if status != 0:
                raise RuntimeError((stderr or rendered or "calibrated Router manifest capture failed").strip())
            if not rendered.strip():
                raise RuntimeError("calibrated Router manifest capture returned empty output")
            effective_content = calibrated_path.read_text(encoding="utf-8")
            execution.update(
                {
                    "router_effective_values": effective_content,
                    "router_effective_values_checksum": text_checksum(effective_content),
                    "router_rendered_manifest": rendered,
                    "router_rendered_manifest_checksum": text_checksum(rendered),
                }
            )
        else:
            helm = await asyncio.create_subprocess_exec(
                str(self._helm_path),
                "upgrade",
                "--install",
                _RELEASE_NAME,
                _ROUTER_CHART,
                "--namespace",
                execution["namespace"],
                "--version",
                _ROUTER_CHART_VERSION,
                "--values",
                str(self._router_base_values),
                "--values",
                str(calibrated_path),
                *router_data_plane_args(execution),
                env=self._environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await helm.communicate()
            if helm.returncode:
                raise RuntimeError(
                    (stderr or stdout).decode(errors="replace").strip() or "router calibration upgrade failed"
                )
        execution["router_values_path"] = str(calibrated_path)
        execution["calibration_output"] = f"PEAK_PREFILL_THROUGHPUT={measured}"

    async def stop(self, execution: dict[str, Any], artifact: GuideDeploymentArtifact) -> dict[str, Any]:
        status, stdout, stderr = await self._runner(
            [
                "kubectl",
                "scale",
                f"deployment/{self._decode_deployment(artifact)}",
                "--namespace",
                execution["namespace"],
                "--replicas=0",
            ]
        )
        return {"stopped": status == 0, "output": stderr or stdout, "error": None}

    async def rollback(self, execution: dict[str, Any], artifact: GuideDeploymentArtifact) -> dict[str, Any]:
        return await self.stop(execution, artifact)

    async def cleanup(
        self, execution: dict[str, Any], artifact: GuideDeploymentArtifact, *, force: bool = False
    ) -> dict[str, Any]:
        helm = await asyncio.create_subprocess_exec(
            str(self._helm_path),
            "uninstall",
            _RELEASE_NAME,
            "--namespace",
            execution["namespace"],
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
                "--wait=false",
                "--ignore-not-found=true",
            ]
        )
        if artifact.manifest_ref:
            shutil.rmtree(Path(artifact.manifest_ref).parent, ignore_errors=True)
        return {"cleaned_up": status == 0, "output": stdout, "error": stderr or None}

    async def _kustomize_build(self, source: Path) -> list[dict[str, Any]]:
        process = await asyncio.create_subprocess_exec(
            "kubectl",
            "kustomize",
            str(source),
            env=self._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise ValueError(f"Kustomize render failed for {source}: {stderr.decode(errors='replace').strip()}")
        return [item for item in yaml.safe_load_all(stdout) if isinstance(item, dict)]

    def _resolve_router_values(self, artifact: GuideDeploymentArtifact) -> Path:
        directory = Path(artifact.manifest_ref or "").parent
        override_path = directory / "router-values-override.yaml"
        if override_path.is_file():
            return override_path
        patched_path = directory / "router-values-patched.yaml"
        if patched_path.is_file():
            return patched_path
        # Raw modelserver-manifest upload (Configuration's `officialGuide.
        # renderedManifest` path): `render()` above never ran, so derive the
        # served model directly from the uploaded Deployment and still patch
        # the EPP's `token-producer.modelName` to match it -- a mismatch is a
        # hard failure per the Guide's own README, not just a perf regression.
        try:
            model = self._extract_model_name(Path(artifact.manifest_ref or "").read_text(encoding="utf-8"))
        except OSError:
            model = None
        if model:
            patched_path.write_text(self._patched_router_values(model, None), encoding="utf-8")
            return patched_path
        return self._router_values

    def _patched_router_values(self, model: str, peak_prefill_throughput: int | None) -> str:
        return self._patched_router_values_content(
            self._router_values.read_text(encoding="utf-8"),
            model,
            peak_prefill_throughput,
        )

    @staticmethod
    def _patched_router_values_content(content: str, model: str, peak_prefill_throughput: int | None) -> str:
        base = yaml.safe_load(content)
        plugins_file = base["router"]["epp"]["pluginsConfigFile"]
        plugins_config = yaml.safe_load(base["router"]["epp"]["pluginsCustomConfig"][plugins_file])
        token_producer = next(item for item in plugins_config["plugins"] if item["type"] == "token-producer")
        token_producer["parameters"]["modelName"] = model
        if peak_prefill_throughput is not None:
            affinity_filter = next(
                item for item in plugins_config["plugins"] if item["type"] == "prefix-cache-affinity-filter"
            )
            affinity_filter["parameters"]["peakPrefillThroughput"] = peak_prefill_throughput
        base["router"]["epp"]["pluginsCustomConfig"][plugins_file] = yaml.safe_dump(plugins_config, sort_keys=False)
        return yaml.safe_dump(base, sort_keys=False)

    @staticmethod
    def _extract_calibration_chunk_size(manifest_text: str) -> int:
        max_num_batched_tokens = 8192
        max_model_len: int | None = None
        for deployment_name, tokens in _modelserver_invocations(manifest_text):
            batched = _option(tokens, {"--max-num-batched-tokens"}, deployment_name)
            context = _option(tokens, {"--max-model-len"}, deployment_name)
            if batched is not None:
                max_num_batched_tokens = int(batched)
            if context is not None:
                max_model_len = int(context)
        # Calibration requests one output token, so the fresh prefill prompt must retain one
        # context token. This makes run1's max-model-len=6000 valid instead of sending 8192.
        return min(max_num_batched_tokens, max_model_len - 1) if max_model_len else max_num_batched_tokens

    @staticmethod
    def _extract_model_name(manifest_text: str) -> str | None:
        for deployment_name, tokens in _modelserver_invocations(manifest_text):
            raw_config = _option(tokens, {"--kv-events-config"}, deployment_name)
            if raw_config is not None:
                try:
                    kv_events = yaml.safe_load(raw_config)
                except yaml.YAMLError:
                    kv_events = None
                topic = str(kv_events.get("topic") or "") if isinstance(kv_events, dict) else ""
                if topic.startswith("kv@") and topic.count("@") >= 2:
                    return topic.split("@", 2)[-1]
            model = _model_argument(tokens, deployment_name)
            if model != "/model-cache":
                return model
        return None

    def _parameters(self, overrides: dict[str, Any]) -> dict[str, Any]:
        model = overrides.get("model") or {}
        decode = overrides.get("decode") or overrides.get("serving") or {}
        runtime = overrides.get("runtime") or {}
        router = overrides.get("router") or {}
        if not isinstance(model.get("name"), str) or not model["name"]:
            raise ValueError("precise-prefix-cache-routing requires model.name")
        if not isinstance(runtime.get("image"), str) or ":" not in runtime["image"]:
            raise ValueError("precise-prefix-cache-routing requires a tagged runtime.image")
        replicas, tensor_parallel = decode.get("replicaCount"), decode.get("tensorParallelSize")
        if not isinstance(replicas, int) or replicas < 1 or not isinstance(tensor_parallel, int) or tensor_parallel < 1:
            raise ValueError(
                "precise-prefix-cache-routing requires positive decode replicaCount and tensorParallelSize"
            )
        peak_prefill_throughput = router.get("peakPrefillThroughput")
        if peak_prefill_throughput is not None and (
            not isinstance(peak_prefill_throughput, (int, float)) or peak_prefill_throughput <= 0
        ):
            raise ValueError("router.peakPrefillThroughput must be a positive number")
        router_values_override = router.get("valuesOverride")
        if router_values_override is not None and not isinstance(router_values_override, str):
            raise ValueError("router.valuesOverride must be a YAML string")
        mount_path = runtime.get("mountPath") or ""
        if not isinstance(mount_path, str) or (mount_path and not mount_path.startswith("/")):
            raise ValueError("runtime.mountPath must be an absolute host path")
        return {
            "model": model["name"],
            "image": runtime["image"],
            "mount_path": mount_path,
            "replicas": replicas,
            "tensor_parallel_size": tensor_parallel,
            "max_model_len": decode.get("maxModelLen"),
            "gpu_memory_utilization": decode.get("gpuMemoryUtilization"),
            "environment": runtime.get("environment") if isinstance(runtime.get("environment"), dict) else {},
            "custom_parameters": overrides.get("customParameters") or [],
            "peak_prefill_throughput": (int(peak_prefill_throughput) if peak_prefill_throughput is not None else None),
            "router_values_override": router_values_override,
        }

    def _patch_modelserver(self, documents: list[dict[str, Any]], parameters: dict[str, Any]) -> None:
        deployment = next((item for item in documents if item.get("kind") == "Deployment"), None)
        claim = next((item for item in documents if item.get("kind") == "ResourceClaimTemplate"), None)
        if deployment is None or (requires_dra_claim(accelerator=self._accelerator) and claim is None):
            raise ValueError(
                "rendered precise-prefix-cache-routing overlay is missing Deployment or ResourceClaimTemplate"
            )
        deployment["spec"]["replicas"] = parameters["replicas"]
        container = next(
            item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "modelserver"
        )
        args = list(container.get("args") or [])
        args[0] = "/model-cache" if parameters["mount_path"] else parameters["model"]
        for index, argument in enumerate(args):
            config_index = index + 1 if argument == "--kv-events-config" else index
            raw_config = (
                args[config_index]
                if argument == "--kv-events-config" and config_index < len(args)
                else str(argument).removeprefix("--kv-events-config=")
                if str(argument).startswith("--kv-events-config=")
                else None
            )
            if raw_config is None:
                continue
            try:
                kv_events = yaml.safe_load(raw_config)
            except yaml.YAMLError:
                continue
            if isinstance(kv_events, dict):
                kv_events["topic"] = f"kv@$(POD_IP):$(POD_PORT)@{parameters['model']}"
                serialized = yaml.safe_dump(kv_events, default_flow_style=True).strip()
                args[config_index] = (
                    serialized if argument == "--kv-events-config" else f"--kv-events-config={serialized}"
                )
        args = _set_argument(args, "tensor-parallel-size", parameters["tensor_parallel_size"])
        if parameters["max_model_len"]:
            args = _set_argument(args, "max-model-len", parameters["max_model_len"])
        if parameters["gpu_memory_utilization"]:
            args = _set_argument(args, "gpu-memory-utilization", parameters["gpu_memory_utilization"])
        environment = list(container.get("env") or [])
        for custom in parameters["custom_parameters"]:
            if custom.get("target") not in {"decode", "both"}:
                continue
            if custom.get("kind") == "argument":
                args = _set_argument(args, custom["name"], custom["value"])
            else:
                environment = _set_environment(environment, custom["name"], custom["value"])
        for name, value in parameters["environment"].items():
            if value:
                environment = _set_environment(environment, name, value)
        for name, value in model_cache_environment("shared-path", cache_mounted=bool(parameters["mount_path"])).items():
            environment = _set_environment(environment, name, value)
        container["args"] = args
        container["env"] = environment
        container["image"] = parameters["image"]
        set_accelerator_request(container, claim, parameters["tensor_parallel_size"], accelerator=self._accelerator)
        if parameters["mount_path"]:
            pod_spec = deployment["spec"]["template"]["spec"]
            pod_spec.setdefault("volumes", []).append(
                {"name": "model-cache", "hostPath": {"path": parameters["mount_path"], "type": "DirectoryOrCreate"}}
            )
            container.setdefault("volumeMounts", []).append(
                {"name": "model-cache", "mountPath": "/model-cache", "readOnly": True}
            )

    def _namespace(self, context: dict[str, Any]) -> str:
        namespace = str(context.get("namespace") or "")
        if not namespace.startswith(self._namespace_prefix) or namespace == self._namespace_prefix:
            raise ValueError("deployment namespace is outside the configured allowlist")
        return namespace


def _set_argument(args: list[str], name: str, value: object) -> list[str]:
    prefix = f"--{name}="
    if any(item.startswith(prefix) for item in args):
        return [f"{prefix}{value}" if item.startswith(prefix) else item for item in args]
    return [*args, f"{prefix}{value}"]


def _set_environment(items: list[dict[str, Any]], name: str, value: object) -> list[dict[str, Any]]:
    if any(item.get("name") == name for item in items):
        return [{"name": name, "value": value} if item.get("name") == name else item for item in items]
    return [*items, {"name": name, "value": value}]
