import pytest

from llm_d_bench.deploy.contracts import (
    ConfigurationArtifact,
    DeploymentCreateRequest,
    DeploymentExecution,
    DeploymentStatus,
)
from llm_d_bench.deploy.providers.guide_adapter import GuideDefinition, GuideDeploymentArtifact, ValidationResult
from llm_d_bench.deploy.providers.guide_catalog import GuideCatalog
from llm_d_bench.deploy.service import ExecutionLogArchive, GuideAdapterDeploymentService


class EvidenceAdapter:
    def discover(self):
        return GuideDefinition("optimized-baseline", "saved", "hash", "stable")

    def validate_inputs(self, *_):
        return ValidationResult(True)

    async def render(self, *_):
        return GuideDeploymentArtifact("optimized-baseline", "hash")

    async def deploy(self, _, context):
        return {
            "namespace": context["namespace"],
            "endpoint_url": "http://router:8000",
            "router_effective_values": "router: {}",
            "router_effective_values_checksum": "values-hash",
            "router_rendered_manifest": "kind: Service",
            "router_rendered_manifest_checksum": "manifest-hash",
        }

    async def readiness(self, _):
        return ValidationResult(True)

    async def diagnostics(self, execution):
        return {key: value for key, value in execution.items() if key.startswith("router_")}


@pytest.mark.asyncio
async def test_installed_router_evidence_survives_ready_execution_serialization_and_restore(tmp_path):
    catalog = GuideCatalog([EvidenceAdapter()])
    service = GuideAdapterDeploymentService(catalog, lambda _: "ns", ExecutionLogArchive(tmp_path))
    request = DeploymentCreateRequest(
        configuration_artifacts=[
            ConfigurationArtifact(
                schema_version="v1",
                kind="helm-values",
                provider_ref="optimized-baseline",
                media_type="application/json",
                checksum="hash",
                content="{}",
            )
        ]
    )
    execution = await service.create(request)
    assert execution.status == DeploymentStatus.READY
    snapshot = execution.diagnostics.value["snapshot"]
    assert snapshot["router_rendered_manifest"] == "kind: Service"
    restored = DeploymentExecution.model_validate_json(execution.model_dump_json())
    restarted = GuideAdapterDeploymentService(catalog, lambda _: "ns", ExecutionLogArchive(tmp_path))
    restarted.restore_execution(restored)
    refreshed = await restarted.refresh_diagnostics(restored)
    assert refreshed.diagnostics.value["snapshot"] == snapshot


@pytest.mark.asyncio
async def test_refresh_captures_calibration_changes_and_restores_them(tmp_path):
    class CalibratingAdapter(EvidenceAdapter):
        async def readiness(self, execution):
            execution.setdefault("calibrated_peak_prefill_throughput", 2500)
            return ValidationResult(True)

        async def diagnostics(self, execution):
            return {
                **await super().diagnostics(execution),
                "calibrated_peak_prefill_throughput": execution.get("calibrated_peak_prefill_throughput"),
            }

    catalog = GuideCatalog([CalibratingAdapter()])
    service = GuideAdapterDeploymentService(catalog, lambda _: "ns", ExecutionLogArchive(tmp_path))
    request = DeploymentCreateRequest(
        configuration_artifacts=[
            ConfigurationArtifact(
                schema_version="v1",
                kind="helm-values",
                provider_ref="optimized-baseline",
                media_type="application/json",
                checksum="hash",
                content="{}",
            )
        ]
    )
    execution = await service.create(request)
    provider_execution = service._provider_executions[execution.execution_id][2]
    provider_execution.pop("calibrated_peak_prefill_throughput")
    refreshed = await service.refresh_diagnostics(execution)
    assert refreshed.diagnostics.value["snapshot"]["calibrated_peak_prefill_throughput"] == 2500
    restarted = GuideAdapterDeploymentService(catalog, lambda _: "ns", ExecutionLogArchive(tmp_path))
    _, _, restored_context = restarted._restored_provider_state(refreshed)
    assert restored_context["calibrated_peak_prefill_throughput"] == 2500
