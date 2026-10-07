"""Tests for the deployment management API: list, metadata edit, and delete."""

import importlib
import json
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse

from llm_d_bench.auth.records import GroupRecord, RoleRecord, UserRecord
from llm_d_bench.auth.service import default_service
from llm_d_bench.db.dao.cluster import ClusterDao
from llm_d_bench.db.dao.group import GroupDao
from llm_d_bench.db.dao.role import RoleDao
from llm_d_bench.db.dao.user import UserDao
from llm_d_bench.deploy.contracts import (
    ConfigurationArtifact,
    DeployableConfiguration,
    DeploymentArtifact,
    DeploymentCase,
    DeploymentCaseStatus,
    DeploymentCreateRequest,
    DeploymentEndpoint,
    DeploymentExecution,
    DeploymentMetadata,
    DeploymentMetadataUpdateRequest,
    DeploymentRun,
    DeploymentRunStatus,
    DeploymentStatus,
    VersionedPayload,
)
from llm_d_bench.deploy.run_store import JsonDeploymentRunStore

router = importlib.import_module("llm_d_bench.deploy.router")
executions = importlib.import_module("llm_d_bench.deploy.executions")
usage = importlib.import_module("llm_d_bench.deploy.usage")


class CleanFailedError(Exception):
    """Provider cleanup failure used to prove records survive a failed delete."""


class FakeDeploymentService:
    """Minimal deployment service that records cleanup calls."""

    def __init__(self, *, fail: bool = False, cleaned_up: bool = False) -> None:
        self.fail = fail
        self.cleaned_up = cleaned_up
        self.cleaned: list[str] = []

    async def clean(self, execution, *, preserve_rendered_overlay: bool = False):
        if self.fail:
            raise CleanFailedError("namespace could not be deleted")
        self.cleaned.append(execution.execution_id)
        status = DeploymentStatus.CLEANED_UP if self.cleaned_up else DeploymentStatus.CLEANED
        return execution.model_copy(update={"status": status, "endpoint": None})


def _artifact(provider_ref: str = "optimized-baseline") -> ConfigurationArtifact:
    return ConfigurationArtifact(
        schema_version="v1",
        kind="helm-values",
        provider_ref=provider_ref,
        checksum="checksum",
        content=json.dumps({"model": {"name": "Qwen/Qwen3-0.6B"}}),
    )


def _case(run_id: str, ordinal: int, execution_id: str, status: DeploymentCaseStatus) -> DeploymentCase:
    return DeploymentCase(
        id=f"case-{ordinal}",
        run_id=run_id,
        ordinal=ordinal,
        source_configuration_ordinal=ordinal,
        provider_ref="optimized-baseline",
        component="modelservice",
        create_request=DeploymentCreateRequest(
            configuration_artifacts=[_artifact()],
            deployment_policy=VersionedPayload(schema_version="v1", value={"deployment_name": f"qwen-{ordinal}"}),
        ),
        status=status,
        execution_id=execution_id,
    )


def _execution(execution_id: str, status: DeploymentStatus, namespace: str) -> DeploymentExecution:
    return DeploymentExecution(
        execution_id=execution_id,
        request_id="request-1",
        status=status,
        artifact=DeploymentArtifact(
            artifact_hash="hash",
            configuration_artifact_ids=["artifact-1"],
            manifest_ref="vllm/manifest.yaml",
            rendered_payload=VersionedPayload(schema_version="v1"),
        ),
        endpoint=(
            DeploymentEndpoint(url=f"http://vllm.{namespace}.svc:8000") if status == DeploymentStatus.READY else None
        ),
        namespace=namespace,
        configuration_artifacts=[_artifact()],
        provenance={"cluster_server_id": "cluster-1", "cluster_session_id": "session-1"},
    )


@pytest.fixture
def deployment_store(tmp_path, monkeypatch):
    """A real JSON store holding one run with two deployments."""
    store = JsonDeploymentRunStore(tmp_path)
    run = DeploymentRun(
        id="run-1",
        status=DeploymentRunStatus.SUCCEEDED,
        source_configurations=[
            DeployableConfiguration(
                type="optimized-baseline",
                format="helm",
                provider_ref="optimized-baseline",
                checksum="checksum",
            )
        ],
        provenance={"cluster_server_id": "cluster-1"},
    )
    run.cases = [
        _case("run-1", 0, "execution-ready", DeploymentCaseStatus.READY),
        _case("run-1", 1, "execution-cleaned", DeploymentCaseStatus.CLEANED),
    ]
    store.create_run(run)
    store.save_execution(_execution("execution-ready", DeploymentStatus.READY, "qwen-ready"))
    store.save_execution(_execution("execution-cleaned", DeploymentStatus.CLEANED, "qwen-cleaned"))
    monkeypatch.setattr(executions, "_store", store)
    executions._execution_locks.clear()
    monkeypatch.setattr(usage, "_probes", {})
    return store


@pytest.fixture
def worker_factory(monkeypatch, deployment_store):
    """Bind the real run worker to a fake provider service."""
    from llm_d_bench.deploy.worker import DeploymentRunWorker

    created: dict[str, object] = {}

    def bind(service, lifecycle_error=None):
        worker = DeploymentRunWorker(deployment_store, service, lifecycle_error)
        created["worker"] = worker
        created["service"] = service
        monkeypatch.setattr(executions.deployment_run_manager, "worker_for_run", lambda _run_id: worker)
        return worker

    return bind


def _payload(response):
    assert isinstance(response, JSONResponse)
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_list_returns_all_deployments_with_metadata_fields(deployment_store):
    response = await router.list_executions()

    identifiers = [item["execution_id"] for item in response["items"]]
    assert sorted(identifiers) == ["execution-cleaned", "execution-ready"]
    for item in response["items"]:
        assert item["display_name"] == ""
        assert item["description"] == ""
        assert "run_id" not in item
        assert "case_id" not in item


@pytest.mark.asyncio
async def test_list_filters_by_query_across_metadata_and_serving_facts(deployment_store):
    await executions.update_execution_metadata(
        "execution-ready", DeploymentMetadataUpdateRequest(display_name="Nightly target")
    )

    by_display_name = await router.list_executions(query="nightly")
    by_namespace = await router.list_executions(query="qwen-cleaned")
    by_model = await router.list_executions(query="qwen3-0.6b")

    assert [item["execution_id"] for item in by_display_name["items"]] == ["execution-ready"]
    assert [item["execution_id"] for item in by_namespace["items"]] == ["execution-cleaned"]
    assert len(by_model["items"]) == 2


@pytest.mark.asyncio
async def test_list_filters_by_multiple_statuses_and_limit(deployment_store):
    both = await router.list_executions(statuses=[DeploymentStatus.READY, DeploymentStatus.CLEANED])
    ready_only = await router.list_executions(statuses=[DeploymentStatus.READY])
    limited = await router.list_executions(limit=1)

    assert len(both["items"]) == 2
    assert [item["execution_id"] for item in ready_only["items"]] == ["execution-ready"]
    assert len(limited["items"]) == 1


@pytest.mark.asyncio
async def test_patch_updates_only_supplied_fields(deployment_store):
    await router.patch_execution(
        "execution-ready",
        DeploymentMetadataUpdateRequest(display_name="  Qwen production  ", description="First"),
    )
    response = await router.patch_execution("execution-ready", DeploymentMetadataUpdateRequest(description="Second"))

    assert response["display_name"] == "Qwen production"
    assert response["description"] == "Second"
    assert deployment_store.get_execution("execution-ready").metadata.display_name == "Qwen production"


@pytest.mark.asyncio
async def test_patch_can_clear_a_field(deployment_store):
    await router.patch_execution("execution-ready", DeploymentMetadataUpdateRequest(description="temporary"))
    response = await router.patch_execution("execution-ready", DeploymentMetadataUpdateRequest(description=""))

    assert response["description"] == ""


@pytest.mark.asyncio
async def test_patch_never_changes_runtime_facts(deployment_store):
    before = deployment_store.get_execution("execution-ready")

    response = await router.patch_execution("execution-ready", DeploymentMetadataUpdateRequest(display_name="renamed"))
    after = deployment_store.get_execution("execution-ready")

    assert response["name"] == "qwen-0"
    assert response["namespace"] == "qwen-ready"
    assert response["status"] == "ready"
    assert after.namespace == before.namespace
    assert after.status == before.status
    assert after.endpoint == before.endpoint
    assert after.provenance == before.provenance


def test_patch_request_rejects_unknown_and_empty_payloads():
    with pytest.raises(ValueError):
        DeploymentMetadataUpdateRequest()
    with pytest.raises(ValueError):
        DeploymentMetadataUpdateRequest(namespace="other")
    with pytest.raises(ValueError):
        DeploymentMetadataUpdateRequest(display_name="x" * 121)


@pytest.mark.asyncio
async def test_patch_reports_missing_deployment(deployment_store):
    response = await router.patch_execution("absent", DeploymentMetadataUpdateRequest(display_name="x"))

    assert response.status_code == 404
    assert _payload(response)["code"] == "deployment_not_found"


@pytest.mark.asyncio
async def test_metadata_survives_lifecycle_execution_saves(deployment_store):
    await executions.update_execution_metadata(
        "execution-ready", DeploymentMetadataUpdateRequest(description="keep me")
    )

    refreshed = deployment_store.get_execution("execution-ready")
    deployment_store.save_execution(refreshed.model_copy(update={"metadata": DeploymentMetadata()}))

    assert deployment_store.get_execution("execution-ready").metadata.description == "keep me"


@pytest.mark.asyncio
async def test_delete_cleans_resources_then_removes_only_that_record(deployment_store, worker_factory):
    service = FakeDeploymentService()
    worker_factory(service)

    await executions.delete_execution("execution-ready")

    assert service.cleaned == ["execution-ready"]
    assert deployment_store.get_execution("execution-ready") is None
    assert deployment_store.get_execution("execution-cleaned") is not None
    run = deployment_store.get_run("run-1")
    assert [case.id for case in run.cases] == ["case-1"]


@pytest.mark.asyncio
async def test_delete_without_namespace_cleanup_keeps_cluster_resources(deployment_store, worker_factory):
    service = FakeDeploymentService()
    worker_factory(service)

    await executions.delete_execution("execution-ready", delete_namespace=False)

    assert service.cleaned == []
    assert deployment_store.get_execution("execution-ready") is None


@pytest.mark.asyncio
async def test_delete_of_cleaned_deployment_skips_cluster_cleanup(deployment_store, worker_factory):
    service = FakeDeploymentService()
    worker_factory(service)

    await executions.delete_execution("execution-cleaned")

    assert service.cleaned == []
    assert deployment_store.get_execution("execution-cleaned") is None


@pytest.mark.asyncio
async def test_delete_removes_the_run_once_its_last_deployment_is_gone(deployment_store, worker_factory):
    worker_factory(FakeDeploymentService())

    await executions.delete_execution("execution-cleaned")
    await executions.delete_execution("execution-ready")

    assert deployment_store.get_run("run-1") is None


def _resource_bindings(*, role_id):
    """Create one user and one group share helper bound to a role."""
    service = default_service()
    suffix = uuid4().hex[:8]
    scope_cluster_id = ClusterDao().create(f"dep-{suffix}", "", "apiVersion: v1\nkind: Config").id
    user = UserDao().create(UserRecord(username=f"dep-user-{suffix}"))
    group = GroupDao().create(GroupRecord(name=f"dep-group-{suffix}"))

    def add(subject_type, resource_type, resource_id):
        kwargs = {
            "scope_type": "resource",
            "scope_cluster_id": scope_cluster_id,
            "scope_resource_type": resource_type,
            "scope_resource_id": resource_id,
        }
        if subject_type == "group":
            return service.add_group_binding(group.id, role_id, **kwargs)
        return service.add_user_binding(user.id, role_id, **kwargs)

    return service, add


@pytest.mark.asyncio
async def test_deleting_a_deployment_revokes_user_and_group_shares(deployment_store, worker_factory):
    worker_factory(FakeDeploymentService())
    role = RoleDao().create(RoleRecord(name="dep-share-role"))
    service, add = _resource_bindings(role_id=role.id)
    add("user", "deployment_execution", "execution-ready")
    add("group", "deployment_execution", "execution-ready")
    add("user", "deployment_execution", "execution-cleaned")
    add("user", "deployment_run", "run-1")

    await executions.delete_execution("execution-ready")

    # The deleted deployment's shares (user and group) are gone.
    assert service.list_resource_shares("deployment_execution", "execution-ready") == []
    # Sibling deployment and the still-existing run keep their shares.
    assert len(service.list_resource_shares("deployment_execution", "execution-cleaned")) == 1
    assert len(service.list_resource_shares("deployment_run", "run-1")) == 1


@pytest.mark.asyncio
async def test_deleting_the_last_deployment_revokes_run_and_execution_shares(deployment_store, worker_factory):
    worker_factory(FakeDeploymentService())
    role = RoleDao().create(RoleRecord(name="dep-share-role"))
    service, add = _resource_bindings(role_id=role.id)
    add("group", "deployment_run", "run-1")
    add("group", "deployment_execution", "execution-ready")

    await executions.delete_execution("execution-cleaned")
    assert len(service.list_resource_shares("deployment_run", "run-1")) == 1

    await executions.delete_execution("execution-ready")

    assert deployment_store.get_run("run-1") is None
    assert service.list_resource_shares("deployment_run", "run-1") == []
    assert service.list_resource_shares("deployment_execution", "execution-ready") == []
    assert deployment_store.list_runs() == []


@pytest.mark.asyncio
async def test_failed_cleanup_keeps_the_deployment_records(deployment_store, worker_factory):
    worker_factory(FakeDeploymentService(fail=True))

    with pytest.raises(CleanFailedError):
        await executions.delete_execution("execution-ready")

    assert deployment_store.get_execution("execution-ready") is not None
    assert len(deployment_store.get_run("run-1").cases) == 2


@pytest.mark.asyncio
async def test_delete_is_refused_when_the_cluster_session_is_gone(deployment_store, worker_factory):
    worker_factory(FakeDeploymentService(), lifecycle_error="cluster session is no longer active")

    response = await router.delete_execution("execution-ready")

    assert response.status_code == 409
    assert _payload(response)["code"] == "deployment_conflict"
    assert deployment_store.get_execution("execution-ready") is not None


@pytest.mark.asyncio
async def test_delete_is_refused_while_a_deployment_is_still_in_use(deployment_store, worker_factory):
    service = FakeDeploymentService()
    worker_factory(service)
    usage.register_usage_probe(
        "evaluate", lambda execution_id: "benchmark run bench-1 is running against this deployment"
    )

    response = await router.delete_execution("execution-ready")

    assert response.status_code == 409
    body = _payload(response)
    assert body["code"] == "deployment_in_use"
    assert "bench-1" in body["detail"]
    assert service.cleaned == []
    assert deployment_store.get_execution("execution-ready") is not None


@pytest.mark.asyncio
async def test_delete_is_refused_when_usage_cannot_be_verified(deployment_store, worker_factory):
    service = FakeDeploymentService()
    worker_factory(service)

    def broken_probe(_execution_id):
        raise RuntimeError("store unreadable")

    usage.register_usage_probe("evaluate", broken_probe)

    response = await router.delete_execution("execution-ready")

    assert response.status_code == 409
    assert service.cleaned == []


@pytest.mark.asyncio
async def test_delete_cancels_and_cleans_up_a_deployment_still_being_created(deployment_store, worker_factory):
    """Deleting a still-deploying execution cancels the run and proceeds with
    normal cleanup instead of being rejected, so users don't have to
    separately cancel and wait before they can delete it.
    """
    service = FakeDeploymentService()
    worker_factory(service)
    run = deployment_store.get_run("run-1")
    run.status = DeploymentRunStatus.RUNNING
    deployment_store.save_run(run)

    response = await router.delete_execution("execution-ready")

    assert response.status_code == 204
    assert service.cleaned == ["execution-ready"]
    assert deployment_store.get_execution("execution-ready") is None
    remaining_run = deployment_store.get_run("run-1")
    assert remaining_run.status not in {
        DeploymentRunStatus.RUNNING,
        DeploymentRunStatus.QUEUED,
        DeploymentRunStatus.CANCELLING,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [DeploymentRunStatus.QUEUED, DeploymentRunStatus.RUNNING, DeploymentRunStatus.CANCELLING],
)
async def test_delete_cancels_every_in_progress_run_status(deployment_store, worker_factory, status):
    service = FakeDeploymentService()
    worker_factory(service)
    run = deployment_store.get_run("run-1")
    run.status = status
    deployment_store.save_run(run)

    response = await router.delete_execution("execution-ready")

    assert response.status_code == 204
    assert service.cleaned == ["execution-ready"]


@pytest.mark.asyncio
async def test_delete_reports_missing_deployment(deployment_store):
    response = await router.delete_execution("absent")

    assert response.status_code == 404
    assert _payload(response)["code"] == "deployment_not_found"


@pytest.mark.asyncio
async def test_delete_run_self_heals_a_case_whose_execution_record_is_gone(deployment_store, worker_factory):
    """A dangling case.execution_id (record removed out of band, e.g. by a

    prior cleanup that crashed before persisting) must not permanently block
    ``delete_run``: the whole point of deleting the run is to get rid of the
    stale reference, not to require it to still resolve.
    """
    service = FakeDeploymentService()
    worker = worker_factory(service)
    run = deployment_store.get_run("run-1")
    run.cases[0].execution_id = "execution-vanished"
    deployment_store.save_run(run)

    await worker.delete_run("run-1")

    assert service.cleaned == []
    assert deployment_store.get_run("run-1") is None


@pytest.mark.asyncio
async def test_delete_endpoint_can_skip_namespace_cleanup(deployment_store, worker_factory):
    service = FakeDeploymentService()
    worker_factory(service)

    response = await router.delete_execution("execution-ready", delete_namespace=False)

    assert response.status_code == 204
    assert service.cleaned == []
    assert deployment_store.get_execution("execution-ready") is None


def test_store_delete_execution_and_atomic_write_leave_no_temporary_files(tmp_path):
    store = JsonDeploymentRunStore(tmp_path)
    store.save_execution(_execution("execution-a", DeploymentStatus.READY, "namespace-a"))
    store.save_execution(_execution("execution-b", DeploymentStatus.READY, "namespace-b"))

    store.delete_execution("execution-a")

    assert store.get_execution("execution-a") is None
    assert store.get_execution("execution-b") is not None
    assert list((tmp_path / "executions").glob("*.tmp")) == []


def test_store_metadata_update_requires_an_existing_execution(tmp_path):
    from llm_d_bench.deploy.run_store import DeploymentRunStoreError

    store = JsonDeploymentRunStore(tmp_path)

    with pytest.raises(DeploymentRunStoreError):
        store.update_execution_metadata("absent", DeploymentMetadataUpdateRequest(display_name="x"))


def test_legacy_execution_records_load_without_metadata(tmp_path):
    store = JsonDeploymentRunStore(tmp_path)
    execution = _execution("execution-legacy", DeploymentStatus.READY, "namespace")
    store.save_execution(execution.model_copy(update={"metadata": DeploymentMetadata()}))

    loaded = store.get_execution("execution-legacy")

    assert loaded is not None
    assert loaded.metadata == DeploymentMetadata()
