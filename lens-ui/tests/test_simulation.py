import asyncio
import importlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import llm_d_bench.simulation.service as simulation_service
from llm_d_bench.api import app
from llm_d_bench.db.dao.simulation_task import SimulationTaskDao
from llm_d_bench.simulation.backends import (
    Backend,
    get_backend,
    list_backends,
    live_completion_timeline_from_artifacts,
    live_summary_from_artifacts,
    register_backend,
    response_code_issues_from_artifacts,
    status_code_breakdown_from_artifacts,
)
from llm_d_bench.simulation.backends import command_for_log as _command_for_log
from llm_d_bench.simulation.backends.aiperf import AIPerfBackend
from llm_d_bench.simulation.backends.trace_replayer import TraceReplayerBackend
from llm_d_bench.simulation.endpoints import models_url
from llm_d_bench.simulation.errors import (
    SimulationCancelledError,
    SimulationConfigurationError,
)
from llm_d_bench.simulation.incluster import resolve_incluster_target
from llm_d_bench.simulation.models import (
    ArtifactRecord,
    BackendCommand,
    BackendDescriptor,
    SimulationArtifact,
    SimulationConfig,
    SimulationDataset,
    SimulationPrompt,
    SimulationRequestResult,
    SimulationResponseCodeIssuePage,
    SimulationResult,
    SimulationTask,
    SimulationTaskCreateRequest,
    SimulationTaskListPage,
    SimulationTrace,
)
from llm_d_bench.simulation.process import CommandResult, RunContext, execute_command
from llm_d_bench.simulation.service import (
    create_task,
    delete_task,
    list_tasks,
    load_task,
    rerun_task,
    reset_service_state,
    save_task,
    stop_task,
)
from llm_d_bench.simulation.traces import (
    BailianTrace,
    BasetenTrace,
    BaseTrace,
    BurstGPTTrace,
    MooncakeTrace,
    WekaPublicDatasetTrace,
    trace_registry,
)


@pytest.fixture
def simulation_root(tmp_path, monkeypatch):
    traces = tmp_path / "datasets"
    traces.mkdir()
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AIPERF_EXECUTABLE", sys.executable)
    monkeypatch.setenv("TRACE_REPLAYER_EXECUTABLE", sys.executable)
    reset_service_state()
    yield tmp_path
    reset_service_state()
    shutil.rmtree(tmp_path, ignore_errors=True)


def task_record(root: Path, task_id: str, status: str = "completed") -> SimulationTask:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return SimulationTask(
        id=task_id,
        name=f"task {task_id}",
        description="",
        scenario="chat",
        status=status,
        endpoint_url="http://127.0.0.1:8000",
        model_name="model",
        simulation=SimulationConfig(
            backend="aiperf",
            backend_options={},
            duration_seconds=60,
            num_requests=None,
            stream=True,
            warmup_enabled=False,
            grace_period_seconds=30,
        ),
        prompt=SimulationPrompt(
            type="trace",
            dataset=SimulationDataset(
                name="mooncake-arxiv",
                scenario="chat",
                tokenizer=None,
            ),
            trace=SimulationTrace(
                path="mooncake_trace.jsonl",
                format="mooncake_trace",
                fixed_schedule=True,
                timeout_seconds=900,
                synthesis_speedup_ratio=1,
            ),
        ),
        task_dir=str(root / "artifacts" / "simulations" / task_id),
        progress_percent=100 if status == "completed" else 0,
        progress_message=status,
        logs=["large log"],
        result=None,
        error_message=None,
        created_at=now,
        started_at=None,
        completed_at=now if status in {"completed", "failed", "cancelled"} else None,
        owner_pid=None,
        owner_instance_id=None,
    )


def update_simulation(task: SimulationTask, **changes) -> None:
    task.simulation = SimulationConfig.model_validate({**task.simulation.model_dump(mode="python"), **changes})


def update_dataset(task: SimulationTask, **changes) -> None:
    dataset = SimulationDataset.model_validate({**task.prompt.dataset.model_dump(mode="python"), **changes})
    task.prompt = task.prompt.model_copy(update={"dataset": dataset}, deep=True)


def update_trace(task: SimulationTask, **changes) -> None:
    trace = SimulationTrace.model_validate({**task.prompt.trace.model_dump(mode="python"), **changes})
    task.prompt = task.prompt.model_copy(update={"trace": trace}, deep=True)


def test_catalog_and_fastapi_contract(simulation_root):
    backends = list_backends()
    assert all(isinstance(backend, BackendDescriptor) for backend in backends)
    assert [backend.name for backend in backends] == ["aiperf", "trace-replayer"]
    assert all(backend.version for backend in backends)
    capabilities = {backend.name: backend.capabilities for backend in backends}
    assert capabilities["aiperf"]["supports_trace_range"] is True
    assert capabilities["aiperf"]["scale_factor_trace_formats"] == ["baseten_trace"]
    assert {option["name"] for option in capabilities["aiperf"]["advanced_options"]} >= {
        "num_profile_runs",
        "concurrency",
        "max_context_length",
    }
    assert capabilities["trace-replayer"]["supports_trace_range"] is True
    assert {
        capability["trace_range_mode"]
        for capability in capabilities["trace-replayer"]["trace_format_capabilities"].values()
    } == {"duration"}
    assert capabilities["trace-replayer"]["scale_factor_trace_formats"] == [
        "mooncake_trace",
        "bailian_trace",
    ]
    assert trace_registry.get("bailian-coder")["scenario"] == "coding"
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/simulation/health").status_code == 200
        assert client.get("/api/simulation/docs").status_code == 200
        assert client.get("/api/simulation/openapi.json").status_code == 200
        assert client.get("/api/simulation/backends").status_code == 200
        scenarios = client.get("/api/simulation/scenarios").json()["scenarios"]
        assert [scenario["name"] for scenario in scenarios] == [
            "chat",
            "api-calling",
            "coding",
        ]
        datasets = client.get("/api/simulation/trace-datasets").json()["datasets"]
        coder = next(item for item in datasets if item["name"] == "bailian-coder")
        assert coder["supported_backends"] == ["aiperf", "trace-replayer"]
        baseten = next(item for item in datasets if item["name"] == "baseten-synthetic")
        assert baseten["supported_backends"] == ["aiperf"]
        weka = next(item for item in datasets if item["name"] == "weka-claude-code")
        assert weka["scenario"] == "coding"
        assert weka["supported_backends"] == ["aiperf"]
        assert weka["source_type"] == "external_dataset"
        assert weka["downloaded"] is True
        burst = next(item for item in datasets if item["name"] == "burstgpt-conversation")
        assert burst["supported_backends"] == ["aiperf"]
        assert not any(item["name"].startswith("azure-") for item in datasets)
        assert (
            client.post(
                "/api/simulation/trace-datasets/download",
                json={"dataset": "unknown", "url": "https://example.com/trace"},
            ).status_code
            == 422
        )
        assert client.get("/api/simulation/tasks?created_after=not-a-date").status_code == 422
        assert (
            client.post(
                "/api/simulation/models/discover",
                json={"endpoint_url": "not-a-url"},
            ).status_code
            == 400
        )


def test_backend_registration_rejects_duplicate_names():
    list_backends()

    class DuplicateBackend(Backend):
        name = "aiperf"
        display_name = "Duplicate"
        trace_formats = []
        capabilities = {}

        async def validate_backend(self, task, context=None):
            return None

        async def run(self, task, context):
            return {}

    with pytest.raises(ValueError, match="already registered"):
        register_backend(DuplicateBackend)


def test_backend_discovery_and_module_boundaries():
    discovered = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from llm_d_bench.simulation.backends.registry import list_backends; "
                "print(json.dumps([item.name for item in list_backends()]))"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(discovered.stdout) == ["aiperf", "trace-replayer"]
    assert AIPerfBackend.__module__ == "llm_d_bench.simulation.backends.aiperf"
    assert TraceReplayerBackend.__module__ == "llm_d_bench.simulation.backends.trace_replayer"
    base_source = Path(importlib.import_module("llm_d_bench.simulation.backends.base").__file__).read_text(
        encoding="utf-8"
    )
    assert "AIPerf" not in base_source
    assert "Trace-Replayer" not in base_source
    assert ".aiperf" not in base_source
    assert ".trace_replayer" not in base_source


def test_non_command_backend_contract():
    class InProcessBackend(Backend):
        name = "in-process"
        display_name = "In-process"
        trace_formats = []
        capabilities = {"prompt_kinds": ["trace"]}

        async def validate_backend(self, task):
            return None

        async def run(self, task, context):
            return SimulationResult(
                run_id=task.id,
                backend="aiperf",
                backend_version="in-process",
                summary={},
                artifacts=[],
                per_request=[],
                backend_metrics={},
                warnings=[],
            )

    backend = InProcessBackend()
    assert backend.descriptor().available is True
    result = asyncio.run(backend.run(task_record(Path("."), "1234abcd"), None))
    assert isinstance(result, SimulationResult)
    assert result.run_id == "1234abcd"


def test_task_details_backfills_backend_version(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    task.result = SimulationResult(
        run_id=task.id,
        backend="trace-replayer",
        backend_version=None,
        summary={},
        artifacts=[],
        per_request=[],
        backend_metrics={},
        warnings=[],
    )
    asyncio.run(save_task(task))

    with TestClient(app) as client:
        response = client.get("/api/simulation/tasks/1234abcd")

    assert response.status_code == 200
    assert response.json()["result"]["backend_version"].startswith("0.2.0+")
    loaded = asyncio.run(load_task(task.id))
    assert loaded is not None
    assert loaded.result is not None
    assert loaded.result.backend_version.startswith("0.2.0+")
    manifest = json.loads((Path(task.task_dir) / "manifest.json").read_text())
    assert manifest["source_version"]["backend_version"] == "unknown"


@pytest.mark.asyncio
async def test_typed_persistence_round_trip_preserves_result_extensions(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    task.result = SimulationResult.model_validate(
        {
            "run_id": task.id,
            "backend": "aiperf",
            "backend_version": "0.12.0",
            "summary": {"custom_summary_metric": {"value": 7}},
            "artifacts": [
                SimulationArtifact(
                    kind="summary",
                    path=str(Path(task.task_dir) / "artifacts" / "summary.json"),
                    media_type="application/json",
                )
            ],
            "per_request": [{"request_id": "request-1", "ttft_ms": None}],
            "backend_metrics": {"custom_backend_metric": True},
            "warnings": [],
            "historical_result_extension": {"kept": True},
        }
    )

    await save_task(task)
    loaded = await load_task(task.id)

    assert isinstance(loaded, SimulationTask)
    assert loaded is not task
    assert isinstance(loaded.result, SimulationResult)
    assert isinstance(loaded.result.artifacts[0], SimulationArtifact)
    assert isinstance(loaded.result.per_request[0], SimulationRequestResult)
    assert loaded.result.summary["custom_summary_metric"] == {"value": 7}
    assert loaded.result.per_request[0].request_id == "request-1"
    assert loaded.result.per_request[0].ttft_ms is None
    assert loaded.result.model_extra["historical_result_extension"] == {"kept": True}
    persisted = SimulationTaskDao().get(task.id)
    assert persisted is not None
    assert persisted.result is not None
    assert persisted.result.model_extra["historical_result_extension"] == {"kept": True}


@pytest.mark.asyncio
async def test_historical_task_json_loads_with_defaults_and_typed_results(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    payload = task.model_dump(mode="json", exclude={"live_summary"})
    payload.pop("endpoint_mode")
    payload.pop("endpoint_namespace")
    payload.pop("endpoint_service")
    payload["prompt"]["trace"].pop("start_seconds")
    payload["prompt"]["trace"].pop("end_seconds")
    payload["result"] = {
        "run_id": task.id,
        "backend": "aiperf",
        "summary": {"ttft": None, "legacy_metric": 9},
        "artifacts": [
            {
                "kind": "summary",
                "path": str(Path(task.task_dir) / "summary.json"),
                "media_type": "application/json",
            }
        ],
        "per_request": [],
        "backend_metrics": {},
        "warnings": [],
        "legacy_parser_detail": "preserve-me",
    }
    directory = Path(task.task_dir)
    directory.mkdir(parents=True)
    (directory / "task.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = await load_task(task.id)

    assert isinstance(loaded, SimulationTask)
    assert loaded.endpoint_mode == "external"
    assert loaded.prompt.trace.start_seconds == 0
    assert loaded.prompt.trace.end_seconds is None
    assert isinstance(loaded.result, SimulationResult)
    assert loaded.result.backend_version is None
    assert loaded.result.summary["ttft"] is None
    assert loaded.result.model_extra["legacy_parser_detail"] == "preserve-me"
    await save_task(loaded)
    (directory / "task.json").unlink()
    round_trip = await load_task(task.id)
    assert round_trip is not None
    assert round_trip.result is not None
    assert round_trip.result.model_extra["legacy_parser_detail"] == "preserve-me"


def test_model_freezing_lifecycle_mutation_and_deep_copy(simulation_root):
    task = task_record(simulation_root, "1234abcd", "queued")
    with pytest.raises(ValidationError):
        task.simulation.duration_seconds = 90
    with pytest.raises(ValidationError):
        task.prompt.trace.path = "other.jsonl"
    with pytest.raises(ValidationError):
        task.status = "invalid"

    copied = task.model_copy(deep=True)
    copied.status = "running"
    copied.logs.append("copy-only")
    update_simulation(copied, backend_options={"threads": 4})

    assert task.status == "queued"
    assert task.logs == ["large log"]
    assert task.simulation.backend_options == {}
    assert copied.status == "running"
    assert copied.simulation.backend_options == {"threads": 4}


@pytest.mark.asyncio
async def test_service_passes_typed_task_to_backend(simulation_root, monkeypatch):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text('{"timestamp":0,"input_length":1,"output_length":1,"hash_ids":[]}\n', encoding="utf-8")
    backend = get_backend("aiperf")
    received = []

    async def validate(task):
        received.append(task)

    monkeypatch.setattr(backend, "validate", validate)
    monkeypatch.setattr(simulation_service, "_schedule_task", lambda _task: None)
    request = SimulationTaskCreateRequest(
        scenario="chat",
        backend="aiperf",
        endpoint_url="http://127.0.0.1:8000",
        model_name="model",
        trace_dataset="mooncake-arxiv",
        trace_path=trace.name,
    )

    task = await create_task(request)

    assert isinstance(task, SimulationTask)
    assert received == [task]
    assert simulation_service.tasks[task.id] is task


def test_create_api_keeps_json_shape_and_passes_typed_request(simulation_root, monkeypatch):
    task = task_record(simulation_root, "1234abcd", "queued")
    received = []

    async def fake_create(request, **_kwargs):
        received.append(request)
        return task

    monkeypatch.setattr(importlib.import_module("llm_d_bench.simulation.router"), "create_task", fake_create)
    with TestClient(app) as client:
        response = client.post(
            "/api/simulation/tasks",
            json={
                "name": "API replay",
                "description": "",
                "scenario": "chat",
                "backend": "aiperf",
                "endpoint_url": "http://127.0.0.1:8000",
                "model_name": "model",
                "trace_dataset": "mooncake-arxiv",
                "trace_path": "mooncake_trace.jsonl",
                "duration_seconds": 60,
                "scale_factor": 1,
                "trace_timeout_seconds": 3600,
                "fixed_schedule": True,
                "stream": True,
                "backend_options": {},
            },
        )

    assert response.status_code == 202
    payload = response.json()
    assert payload["task_id"] == task.id
    assert payload["status"] == "queued"
    assert payload["task"]["id"] == task.id
    assert payload["task"]["simulation"]["backend"] == "aiperf"
    assert "live_summary" not in payload["task"]
    assert len(received) == 1
    assert isinstance(received[0], SimulationTaskCreateRequest)


def test_models_url_normalization():
    assert models_url("http://localhost:8000") == "http://localhost:8000/v1/models"
    assert models_url("https://example.com/v1") == "https://example.com/v1/models"
    assert models_url("https://example.com/v1/chat/completions?ignored=true") == "https://example.com/v1/models"


def test_command_log_quotes_arguments_and_redacts_url_credentials():
    command = _command_for_log(
        "/opt/trace replayer",
        [
            "--endpoint",
            "https://user:secret@example.com:8443/v1/chat/completions?token=secret#fragment",
            "--model-name",
            "model name",
        ],
    )

    assert command == (
        "'/opt/trace replayer' --endpoint https://example.com:8443/v1/chat/completions --model-name 'model name'"
    )
    assert "secret" not in command
    assert "user" not in command


@pytest.mark.parametrize(
    ("environment_name", "backend_type"),
    [
        (
            "TRACE_REPLAYER_EXECUTABLE",
            TraceReplayerBackend,
        ),
        ("AIPERF_EXECUTABLE", AIPerfBackend),
    ],
)
@pytest.mark.asyncio
async def test_backend_installs_its_executable(
    simulation_root,
    monkeypatch,
    environment_name,
    backend_type,
):
    monkeypatch.delenv(environment_name)
    monkeypatch.setenv(
        "SIMULATION_BACKEND_CACHE_DIR",
        str(simulation_root / "backend-cache"),
    )
    if environment_name == "AIPERF_EXECUTABLE":
        monkeypatch.setattr(
            importlib.import_module(backend_type.__module__).sys,
            "executable",
            str(simulation_root / "runtime" / "bin" / "python"),
        )
    monkeypatch.setattr(
        importlib.import_module(backend_type.__module__).shutil,
        "which",
        lambda executable: None,
    )
    backend = backend_type()
    destination = backend.managed_executable_path()

    def install(path):
        assert path == destination
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(backend_type, "_install", staticmethod(install))
    logs = []
    context = RunContext(asyncio.Event(), logs.append, lambda *_args: None)
    executable = await backend.prepare_executable(context)

    assert executable == str(destination)
    assert destination.is_file()
    assert "install" in logs[0].lower()
    assert str(destination) in logs[1]


@pytest.mark.parametrize("backend", [AIPerfBackend(), TraceReplayerBackend()])
@pytest.mark.asyncio
async def test_all_command_backends_log_the_launch_command(
    simulation_root,
    monkeypatch,
    backend,
):
    task = task_record(simulation_root, "1234abcd")
    update_simulation(task, backend=backend.name)
    if backend.name == "aiperf":
        update_trace(task, start_seconds=10, end_seconds=20)
    executable = sys.executable
    logs = []

    async def validate(_task):
        return None

    async def prepare(_context):
        return executable

    async def command(_task):
        args = ["--version"]
        if backend.name == "aiperf":
            args.extend(["--input-file", str(simulation_root / "selected_trace.parquet")])
        return BackendCommand(args=tuple(args), timeout_seconds=10)

    async def parse(_task, _result, _version):
        return SimulationResult(
            run_id=_task.id,
            backend=backend.name,
            backend_version=_version,
            summary={},
            artifacts=[],
            per_request=[],
            backend_metrics={},
            warnings=[],
        )

    async def execute(*_args, **_kwargs):
        root = Path(task.task_dir)
        root.mkdir(parents=True)
        now = datetime.now(UTC)
        return CommandResult(0, now, now, root / "stdout.log", root / "stderr.log")

    monkeypatch.setattr(backend, "validate", validate)
    monkeypatch.setattr(backend, "prepare_executable", prepare)
    monkeypatch.setattr(backend, "command", command)
    monkeypatch.setattr(backend, "parse", parse)
    monkeypatch.setattr(backend, "expected_request_count", lambda _task: 1)
    monkeypatch.setattr("llm_d_bench.simulation.backends.base.execute_command", execute)
    context = RunContext(asyncio.Event(), logs.append, lambda *_args: None)

    await backend.run(task, context)

    assert any(log.startswith(f"Command: {executable} --version") for log in logs)
    if backend.name == "aiperf":
        assert any(
            "Replay range: 10.000s-20.000s source time" in log
            and f"input={simulation_root / 'selected_trace.parquet'}" in log
            for log in logs
        )


@pytest.mark.asyncio
async def test_rerun_copies_configuration(simulation_root, monkeypatch):
    original = task_record(simulation_root, "1234abcd")
    original.name = "production replay"
    original.endpoint_mode = "in-cluster"
    original.endpoint_namespace = "serving"
    original.endpoint_service = "gateway"
    original.simulation = SimulationConfig(
        backend="aiperf",
        backend_options={"threads": 4},
        duration_seconds=12.5,
        num_requests=None,
        stream=True,
        warmup_enabled=False,
        grace_period_seconds=7.5,
    )
    update_dataset(original, tokenizer="/models/tokenizer.json")
    update_trace(original, start_seconds=10, end_seconds=40, timeout_seconds=90.5)
    await save_task(original)
    monkeypatch.setattr("llm_d_bench.simulation.service._schedule_task", lambda _task: None)

    class AvailableBackend:
        async def validate(self, _task):
            return None

    monkeypatch.setattr(simulation_service, "get_backend", lambda _name: AvailableBackend())
    rerun = await rerun_task(original.id)
    assert rerun.name == f"production replay · rerun {rerun.id}"
    assert rerun.endpoint_mode == "in-cluster"
    assert rerun.endpoint_namespace == "serving"
    assert rerun.prompt.dataset.tokenizer == "/models/tokenizer.json"
    assert rerun.prompt.trace.start_seconds == 10
    assert rerun.prompt.trace.end_seconds == 40
    assert rerun.prompt.trace.timeout_seconds == 90.5
    assert rerun.simulation.duration_seconds == 12.5
    assert rerun.simulation.grace_period_seconds == 7.5
    assert rerun.simulation.backend_options == {"threads": 4}
    assert rerun.result is None
    assert rerun.logs == []


@pytest.mark.asyncio
async def test_stop_is_idempotent_for_terminal_tasks(simulation_root):
    for index, status in enumerate(("completed", "failed", "cancelled")):
        task = task_record(simulation_root, f"1234abc{index}", status)
        await save_task(task)
        await stop_task(task.id)
        loaded = await load_task(task.id)
        assert loaded.status == status


def test_simulation_config_backend_options_are_immutable():
    config = SimulationConfig(
        backend="aiperf",
        backend_options={"nested": {"percentiles": [90, 99]}},
        duration_seconds=60,
        num_requests=None,
        stream=True,
        warmup_enabled=False,
        grace_period_seconds=30,
    )

    with pytest.raises(TypeError, match="cannot be modified"):
        config.backend_options["threads"] = 4
    with pytest.raises(TypeError, match="cannot be modified"):
        config.backend_options["nested"]["threads"] = 4
    with pytest.raises(TypeError):
        config.backend_options["nested"]["percentiles"][0] = 50
    assert config.model_dump(mode="json")["backend_options"] == {"nested": {"percentiles": [90, 99]}}


@pytest.mark.asyncio
async def test_queued_tasks_run_in_creation_order(simulation_root, monkeypatch):
    monkeypatch.setenv("SIMULATION_MAX_CONCURRENT_TASKS", "1")
    first = task_record(simulation_root, "11111111", "queued")
    second = task_record(simulation_root, "22222222", "queued")
    started = []
    first_release = asyncio.Event()
    second_release = asyncio.Event()

    async def fake_run(task):
        task.status = "running"
        started.append(task.id)
        await (first_release if task.id == first.id else second_release).wait()
        task.status = "completed"

    monkeypatch.setattr(simulation_service, "_run_task", fake_run)
    simulation_service._schedule_task(first)
    simulation_service._schedule_task(second)
    scheduled = list(simulation_service.runners.values())
    await asyncio.sleep(0.05)
    assert started == ["11111111"]
    assert second.status == "queued"

    first_release.set()
    for _ in range(20):
        if len(started) == 2:
            break
        await asyncio.sleep(0.01)
    assert started == ["11111111", "22222222"]
    second_release.set()
    await asyncio.gather(*scheduled)


@pytest.mark.asyncio
async def test_managed_tokenizer_uses_model_cache(simulation_root, monkeypatch):
    monkeypatch.delenv("TRACE_REPLAYER_TOKENIZER", raising=False)
    monkeypatch.setenv(
        "SIMULATION_TOKENIZER_CACHE_DIR",
        str(simulation_root / "tokenizers"),
    )
    cached = TraceReplayerBackend.tokenizer_cache_path("organization/model")
    cached.mkdir(parents=True)
    (cached / "tokenizer.json").write_text('{"version":"1"}\n', encoding="utf-8")
    (cached / "tokenizer_config.json").write_text('{"model_max_length":4096}\n', encoding="utf-8")
    tokenizer, config = await TraceReplayerBackend.ensure_tokenizer("organization/model")
    assert tokenizer == cached / "tokenizer.json"
    assert config == cached / "tokenizer_config.json"


@pytest.mark.asyncio
async def test_backend_scenario_dataset_file_binding(simulation_root):
    trace = simulation_root / "datasets" / "qwen_coder_blksz_16.jsonl"
    trace.write_text("{}\n", encoding="utf-8")
    request = SimulationTaskCreateRequest(
        name="mismatch",
        description="",
        scenario="chat",
        backend="aiperf",
        endpoint_url="http://127.0.0.1:8000",
        model_name="model",
        trace_dataset="bailian-coder",
        trace_path=trace.name,
        duration_seconds=60,
        scale_factor=1,
        trace_timeout_seconds=3600,
        fixed_schedule=True,
        stream=True,
        backend_options={},
    )
    with pytest.raises(SimulationConfigurationError, match="belongs to scenario 'coding'"):
        await create_task(request)
    request = request.model_copy(update={"scenario": "coding", "trace_path": "other.jsonl"})
    with pytest.raises(SimulationConfigurationError, match="does not match"):
        await create_task(request)
    request = request.model_copy(update={"trace_path": trace.name, "scale_factor": 2})
    with pytest.raises(SimulationConfigurationError, match="does not support scale_factor"):
        await create_task(request)
    request = request.model_copy(
        update={
            "backend": "trace-replayer",
            "scale_factor": 1,
            "trace_start_seconds": 1,
            "trace_end_seconds": 10,
        }
    )
    with pytest.raises(SimulationConfigurationError, match="requires trace_start_seconds to be 0"):
        await create_task(request)


@pytest.mark.parametrize("backend", [AIPerfBackend(), TraceReplayerBackend()])
@pytest.mark.asyncio
async def test_backends_validate_error_rate_slo(simulation_root, backend):
    task = task_record(simulation_root, "1234abcd")
    update_simulation(task, backend=backend.name, backend_options={"error_rate_slo": 101})
    with pytest.raises(
        SimulationConfigurationError,
        match="error_rate_slo must be a percentage from 0 to 100",
    ):
        await backend.validate(task)


@pytest.mark.asyncio
async def test_persistence_listing_recovery_and_safe_deletion(simulation_root):
    completed = task_record(simulation_root, "11111111")
    completed.created_at = "2026-01-01T00:00:00.000Z"
    interrupted = task_record(simulation_root, "22222222", "running")
    interrupted.created_at = "2026-01-02T00:00:00.000Z"
    interrupted.model_name = "other-model"
    interrupted.endpoint_url = "http://inference.example/v1"
    update_dataset(interrupted, name="other-dataset")
    await asyncio.gather(save_task(completed), save_task(interrupted))
    listed = await list_tasks(limit=100)
    assert isinstance(listed, SimulationTaskListPage)
    assert [task.id for task in listed.tasks] == ["22222222", "11111111"]
    assert listed.tasks[0].logs == []
    assert listed.facets["models"] == ["model", "other-model"]
    second_page = await list_tasks(offset=1, limit=1)
    assert second_page.offset == 1
    assert second_page.limit == 1
    assert [task.id for task in second_page.tasks] == ["11111111"]
    assert (await list_tasks(model="other-model", limit=1)).total == 1
    assert (await list_tasks(dataset="other-dataset", limit=1)).total == 1
    assert (await list_tasks(endpoint="INFERENCE.EXAMPLE", limit=1)).total == 1
    assert (await list_tasks(created_after="2026-01-01T12:00:00+00:00", limit=1)).total == 1
    recovered = await load_task("22222222")
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.progress_message == "Simulation interrupted by service restart"
    await delete_task("11111111")
    assert not Path(completed.task_dir).exists()
    assert await load_task("11111111") is None


def test_trace_path_and_format_validation(simulation_root):
    trace = simulation_root / "datasets" / "valid.jsonl"
    trace.write_text(
        '{"timestamp": 1, "input_length": 2, "output_length": 3}\n',
        encoding="utf-8",
    )
    MooncakeTrace(trace).validate()
    assert isinstance(trace_registry.resolve(trace.name, "mooncake_trace"), MooncakeTrace)
    assert isinstance(trace_registry.resolve(trace.name, "bailian_trace"), BailianTrace)
    assert isinstance(trace_registry.resolve(trace.name, "baseten_trace"), BasetenTrace)
    assert isinstance(trace_registry.resolve(trace.name, "burst_gpt_trace"), BurstGPTTrace)
    assert isinstance(
        trace_registry.resolve("semianalysis_cc_traces_weka_no_subagents", "weka_public_dataset"),
        WekaPublicDatasetTrace,
    )
    outside = simulation_root / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SimulationConfigurationError, match="inside"):
        trace_registry.resolve("../outside.jsonl", "mooncake_trace")
    lfs = simulation_root / "datasets" / "lfs.jsonl"
    lfs.write_text("version https://git-lfs.github.com/spec/v1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Git LFS pointer"):
        MooncakeTrace(lfs).validate()


def test_timeline_index(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "input_length": 2,
                    "output_length": 3,
                    "hash_ids": [],
                }
            )
            for timestamp in [0, 500, 1000, 1500, 2000]
        )
        + "\n",
        encoding="utf-8",
    )
    timeline = MooncakeTrace(trace).timeline(3)
    assert timeline["duration_seconds"] == 2
    assert timeline["request_count"] == 5
    assert sum(item["request_count"] for item in timeline["bins"]) == 5

    with TestClient(app) as client:
        response = client.get("/api/simulation/trace-datasets/mooncake-arxiv/timeline?bins=50")
    assert response.status_code == 200
    assert response.json()["duration_seconds"] == 2


@pytest.mark.asyncio
async def test_baseten_dataset_uses_native_range_with_replay_speedup(simulation_root):
    result = await BaseTrace.download("baseten-synthetic")
    source = Path(result["path"])
    index = BasetenTrace(source).build_timeline_index()
    assert index["request_count"] == 600
    assert index["duration_seconds"] == 599

    task = task_record(simulation_root, "1234abcd")
    update_trace(
        task,
        path=source.name,
        format="baseten_trace",
        start_seconds=10,
        end_seconds=20,
        synthesis_speedup_ratio=2,
    )
    command = await AIPerfBackend().command(task)
    assert isinstance(command, BackendCommand)
    args = command.args
    assert args[args.index("--custom-dataset-type") + 1] == "baseten_trace"
    assert args[args.index("--replay-speedup") + 1] == "2.0"
    assert "--synthesis-speedup-ratio" not in args
    assert Path(args[args.index("--input-file") + 1]) == source
    assert args[args.index("--fixed-schedule-start-offset") + 1] == "10000"
    assert args[args.index("--fixed-schedule-end-offset") + 1] == "20000"

    update_trace(task, synthesis_speedup_ratio=1)
    native_command = await AIPerfBackend().command(task)
    native_args = native_command.args
    assert Path(native_args[native_args.index("--input-file") + 1]) == source
    assert native_args[native_args.index("--fixed-schedule-start-offset") + 1] == "10000"
    assert native_args[native_args.index("--fixed-schedule-end-offset") + 1] == "20000"


@pytest.mark.asyncio
async def test_aiperf_command_passes_api_key_and_redacts_it_in_logs(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        json.dumps({"timestamp": 0, "input_length": 2, "output_length": 3, "hash_ids": []}) + "\n",
        encoding="utf-8",
    )
    task = task_record(simulation_root, "1234abcd")
    update_trace(task, path=trace.name)
    task.api_key = "lens-mk-super-secret"
    # The run-only token must never be serialized (persisted or returned by the API).
    assert "api_key" not in task.model_dump()
    args = (await AIPerfBackend().command(task)).args
    assert args[args.index("--api-key") + 1] == "lens-mk-super-secret"
    logged = _command_for_log("aiperf", args)
    assert "lens-mk-super-secret" not in logged
    assert "<redacted>" in logged


@pytest.mark.asyncio
async def test_aiperf_weka_public_dataset_command(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    task.scenario = "coding"
    update_dataset(task, name="weka-claude-code", scenario="coding")
    update_trace(
        task,
        path="semianalysis_cc_traces_weka_no_subagents",
        format="weka_public_dataset",
        fixed_schedule=False,
    )
    update_simulation(
        task,
        backend_options={
            "random_seed": 42,
            "num_profile_runs": 2,
            "profile_run_cooldown_seconds": 3,
            "concurrency": 4,
            "max_context_length": 262144,
            "trace_idle_gap_cap_seconds": 10,
            "ttft_slo": 0.5,
            "tpot_slo": 0.05,
            "error_rate_slo": 1.5,
        },
    )
    backend = AIPerfBackend()
    await backend.validate(task)
    args = (await backend.command(task)).args
    assert args[args.index("--public-dataset") + 1] == ("semianalysis_cc_traces_weka_no_subagents")
    assert args[args.index("--concurrency") + 1] == "4"
    assert args[args.index("--max-context-length") + 1] == "262144"
    assert args[args.index("--trace-idle-gap-cap-seconds") + 1] == "10"
    assert args[args.index("--num-profile-runs") + 1] == "2"
    assert args[args.index("--profile-run-cooldown-seconds") + 1] == "3"
    assert args[args.index("--random-seed") + 1] == "42"
    assert "--ttft-slo" not in args
    assert "--tpot-slo" not in args
    assert "--error-rate-slo" not in args
    assert "--input-file" not in args
    assert "--fixed-schedule" not in args


@pytest.mark.asyncio
async def test_burstgpt_filter_timeline_and_aiperf_command(simulation_root):
    source = simulation_root / "burstgpt.csv"
    source.write_text(
        "Timestamp,Model,Request tokens,Response tokens,Total tokens,Log Type\n"
        "5,ChatGPT,100,20,120,Conversation log\n"
        "7,ChatGPT,200,30,230,API log\n"
        "15,ChatGPT,300,40,340,Conversation log\n",
        encoding="utf-8",
    )
    selected = simulation_root / "datasets" / "burstgpt_conversation.csv"
    trace_registry.get_variant("burstgpt-conversation").transform_download(
        source,
        selected,
    )
    trace = BurstGPTTrace(selected)
    trace.validate()
    index = trace.build_timeline_index()
    assert index["request_count"] == 2
    assert index["duration_seconds"] == 10
    assert "API log" not in selected.read_text(encoding="utf-8")

    task = task_record(simulation_root, "1234abcd")
    update_trace(task, path=selected.name, format="burst_gpt_trace")
    backend = AIPerfBackend()
    await backend.validate(task)
    args = (await backend.command(task)).args
    assert args[args.index("--custom-dataset-type") + 1] == "burst_gpt_trace"
    assert args[args.index("--input-file") + 1] == str(selected)


@pytest.mark.asyncio
async def test_task_range_is_persisted_and_drives_duration(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "input_length": 2,
                    "output_length": 3,
                    "hash_ids": [],
                }
            )
            for timestamp in [0, 1000, 2000, 3000]
        )
        + "\n",
        encoding="utf-8",
    )
    task = await create_task(
        SimulationTaskCreateRequest(
            name="range",
            description="",
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://127.0.0.1:8000",
            model_name="model",
            trace_dataset="mooncake-arxiv",
            trace_path=trace.name,
            duration_seconds=99,
            scale_factor=1,
            trace_start_seconds=0.5,
            trace_end_seconds=2.5,
            trace_timeout_seconds=3600,
            fixed_schedule=True,
            stream=True,
            backend_options={},
        )
    )
    task.status = "cancelled"
    assert task.simulation.duration_seconds == 2
    assert task.prompt.trace.start_seconds == 0.5
    assert task.prompt.trace.end_seconds == 2.5


@pytest.mark.asyncio
async def test_aiperf_command_uses_native_range_offsets(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "input_length": 2,
                    "output_length": 3,
                    "hash_ids": [],
                }
            )
            for timestamp in [0, 500, 1000, 1500, 2000]
        )
        + "\n",
        encoding="utf-8",
    )
    task = task_record(simulation_root, "1234abcd")
    update_simulation(
        task,
        backend_options={
            "num_profile_runs": 1,
            "profile_run_cooldown_seconds": 0,
        },
    )
    update_trace(task, path=trace.name, start_seconds=0.5, end_seconds=1.5)
    args = (await AIPerfBackend().command(task)).args
    assert Path(args[args.index("--input-file") + 1]) == trace
    assert args[args.index("--fixed-schedule-start-offset") + 1] == "500"
    assert args[args.index("--fixed-schedule-end-offset") + 1] == "1500"
    assert not (Path(task.task_dir) / "artifacts" / "aiperf").exists()
    assert "--profile-run-cooldown-seconds" not in args


@pytest.mark.asyncio
async def test_trace_replayer_normalizes_mooncake_trace(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "input_length": 2,
                    "output_length": 3,
                    "hash_ids": [],
                }
            )
            for timestamp in [0, 500, 1000]
        )
        + "\n",
        encoding="utf-8",
    )
    task = task_record(simulation_root, "1234abcd")
    update_trace(task, path=trace.name, start_seconds=0, end_seconds=None)
    selected = await TraceReplayerBackend()._trace_path(task)
    records = [json.loads(line) for line in selected.read_text(encoding="utf-8").splitlines()]
    assert [record["timestamp"] for record in records] == [0, 0.5, 1]


@pytest.mark.asyncio
async def test_trace_replayer_command_uses_producer_defaults(simulation_root, monkeypatch):
    task = task_record(simulation_root, "1234abcd")
    update_simulation(task, backend="trace-replayer")
    backend = TraceReplayerBackend()

    async def tokenizer_paths(_task):
        return Path("/tokenizer.json"), Path("/tokenizer_config.json")

    async def trace_path(_task):
        return Path("/trace.jsonl")

    monkeypatch.setattr(backend, "_tokenizer_paths", tokenizer_paths)
    monkeypatch.setattr(backend, "_trace_path", trace_path)

    args = (await backend.command(task)).args

    assert args[args.index("--num-producer") + 1] == "16"
    assert args[args.index("--channel-capacity") + 1] == "32"
    assert args[args.index("--threads") + 1] == "32"


@pytest.mark.asyncio
async def test_trace_replayer_passes_api_key_as_environment(simulation_root, monkeypatch):
    task = task_record(simulation_root, "1234abcd")
    update_simulation(task, backend="trace-replayer")
    task.api_key = "lens-mk-super-secret"
    backend = TraceReplayerBackend()

    async def tokenizer_paths(_task):
        return Path("/tokenizer.json"), Path("/tokenizer_config.json")

    async def trace_path(_task):
        return Path("/trace.jsonl")

    monkeypatch.setattr(backend, "_tokenizer_paths", tokenizer_paths)
    monkeypatch.setattr(backend, "_trace_path", trace_path)

    command = await backend.command(task)
    assert command.env == (("OPENAI_API_KEY", "lens-mk-super-secret"),)
    # The token is never an argv entry, so it cannot leak into the logged command.
    assert "lens-mk-super-secret" not in _command_for_log("trace-replayer", command.args)


@pytest.mark.asyncio
async def test_trace_replayer_duration_does_not_slice_trace(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "input_length": 2,
                    "output_length": 3,
                    "hash_ids": [],
                }
            )
            for timestamp in [0, 500, 1000, 1500, 2000]
        )
        + "\n",
        encoding="utf-8",
    )
    task = task_record(simulation_root, "1234abcd")
    update_simulation(task, backend="trace-replayer")
    update_trace(task, path=trace.name, start_seconds=0, end_seconds=1.5)
    selected = await TraceReplayerBackend()._trace_path(task)
    records = [json.loads(line) for line in selected.read_text(encoding="utf-8").splitlines()]
    assert [record["timestamp"] for record in records] == [0, 0.5, 1, 1.5, 2]


def test_backend_expected_request_counts_follow_end_boundary_semantics(simulation_root):
    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps({"timestamp": timestamp, "input_length": 8, "output_length": 2})
            for timestamp in (1000, 2000, 3000)
        )
        + "\n",
        encoding="utf-8",
    )
    task = task_record(simulation_root, "1234abcd")
    update_trace(task, path=trace.name, start_seconds=0, end_seconds=2)

    update_simulation(task, backend="aiperf", backend_options={"num_profile_runs": 2})
    assert AIPerfBackend().expected_request_count(task) == 4

    update_simulation(task, backend="trace-replayer", backend_options={})
    assert TraceReplayerBackend().expected_request_count(task) == 3


@pytest.mark.asyncio
async def test_process_cancellation_and_runner_cancellation_terminate_child(simulation_root):
    context = RunContext(
        cancel_event=asyncio.Event(),
        log=lambda _message: None,
        progress=lambda _percent, _message: None,
    )
    running = asyncio.create_task(
        execute_command(
            sys.executable,
            ["-c", "import time; time.sleep(60)"],
            simulation_root / "cancel",
            60,
            context,
        )
    )
    await asyncio.sleep(0.1)
    context.cancel_event.set()
    with pytest.raises(SimulationCancelledError):
        await running
    assert context.process is None

    context = RunContext(
        cancel_event=asyncio.Event(),
        log=lambda _message: None,
        progress=lambda _percent, _message: None,
    )
    running = asyncio.create_task(
        execute_command(
            sys.executable,
            ["-c", "import time; time.sleep(60)"],
            simulation_root / "runner-cancel",
            60,
            context,
        )
    )
    await asyncio.sleep(0.1)
    process = context.process
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert process is not None and process.returncode is not None


@pytest.mark.asyncio
async def test_process_reports_progress_without_expected_request_count(simulation_root):
    updates = []
    context = RunContext(
        cancel_event=asyncio.Event(),
        log=lambda _message: None,
        progress=lambda percent, message: updates.append((percent, message)),
    )

    await execute_command(
        sys.executable,
        ["-c", "import time; time.sleep(0.1)"],
        simulation_root / "unknown-request-count",
        10,
        context,
        progress_request_counts=lambda: (7, None),
    )

    assert updates
    assert updates[0][0] == 0
    assert "7 requests completed" in updates[0][1]


@pytest.mark.asyncio
async def test_trace_replayer_schedule_drift_statistics(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    update_simulation(
        task,
        backend="trace-replayer",
        backend_options={"ttft_slo": 0.075, "tpot_slo": 0.015},
    )
    artifacts = Path(task.task_dir) / "artifacts" / "trace-replayer"
    artifacts.mkdir(parents=True)
    stdout = artifacts / "stdout.log"
    stderr = artifacts / "stderr.log"
    stdout.write_text(
        "2026-08-12T00:00:01Z ERROR Request#3::(64|8) error: "
        "error sending request for url (http://endpoint/v1/chat/completions)\n",
        encoding="utf-8",
    )
    stderr.write_text("", encoding="utf-8")
    (artifacts / "summary.json").write_text(
        json.dumps(
            {
                "requests_total": "2",
                "requests_success": "1",
                "output_tokens_total": "64",
                "duration_ms": "2000",
            }
        ),
        encoding="utf-8",
    )
    (artifacts / "requests.jsonl").write_text(
        '{"input_length": "128", "output_length": "32", "status": "200", "s_time": "100", "s_time_drift": "1.5", "e_time": "500", "first_token_time": "50", "avg_time_between_tokens": "10"}\n'
        '{"input_length": "256", "output_length": "64", "status": "500", "s_time": "750", "s_time_drift": "3.5", "e_time": "1500", "first_token_time": "100", "avg_time_between_tokens": "20"}\n',
        encoding="utf-8",
    )
    now = datetime.now(UTC)
    result = await TraceReplayerBackend().parse(
        task,
        CommandResult(0, now, now, stdout, stderr),
        "test",
    )
    assert isinstance(result, SimulationResult)
    assert all(isinstance(artifact, SimulationArtifact) for artifact in result.artifacts)
    assert result.summary["total_input_tokens"] == 384
    assert result.summary["total_requests"] == 3
    assert result.summary["failed_requests"] == 2
    assert result.summary["client_error_requests"] == 1
    assert result.summary["drift"]["mean_ms"] == 2.5
    assert result.summary["drift"]["p95_ms"] == 3.5
    assert sum(item["arrived_requests"] for item in result.summary["completion_timeline"]) == 2
    assert sum(item["completed_requests"] for item in result.summary["completion_timeline"]) == 2
    assert sum(item["successful_requests"] for item in result.summary["completion_timeline"]) == 1
    assert sum(item["failed_requests"] for item in result.summary["completion_timeline"]) == 1
    assert result.summary["status_code_breakdown"] == [
        {"status_code": 500, "count": 1, "percentage": 50},
        {"status_code": None, "count": 1, "percentage": 50},
    ]
    assert len(TraceReplayerBackend().artifact_records(task)) == 3
    assert result.summary["completion_timeline"][-1]["cumulative_arrived"] == 2
    assert result.summary["completion_timeline"][-1]["cumulative_completed"] == 2
    latency_timeline = result.summary["latency_timeline"]
    assert sum(item["request_count"] for item in latency_timeline) == 2
    assert [item["average_latency_ms"] for item in latency_timeline if item["request_count"]] == [
        400,
        750,
    ]
    assert sum(item["request_count"] for item in result.summary["ttft_timeline"]) == 2
    assert sum(item["request_count"] for item in result.summary["tpot_timeline"]) == 2
    ttft_heatmap = result.summary["ttft_heatmap"]
    assert ttft_heatmap["metric"] == "ttft"
    assert ttft_heatmap["sequence_length"] == "input"
    assert ttft_heatmap["sequence_length_source"] == "observed"
    assert sum(item["request_count"] for item in ttft_heatmap["cells"]) == 2
    assert {item["sequence_length_start"] for item in ttft_heatmap["cells"]} == {128, 192}
    tpot_heatmap = result.summary["tpot_heatmap"]
    assert tpot_heatmap["metric"] == "tpot"
    assert tpot_heatmap["sequence_length"] == "output"
    assert tpot_heatmap["sequence_length_source"] == "requested"
    assert sum(item["request_count"] for item in tpot_heatmap["cells"]) == 2
    assert {item["sequence_length_start"] for item in tpot_heatmap["cells"]} == {32, 48}
    throughput_timeline = result.summary["throughput_timeline"]
    assert sum(item["arrived_requests"] for item in throughput_timeline) == 2
    assert sum(item["completed_requests"] for item in throughput_timeline) == 2
    assert sum(item["output_tokens"] for item in throughput_timeline) == 96
    goodput_timeline = result.summary["goodput_timeline"]
    assert sum(item["completed_requests"] for item in goodput_timeline) == 2
    assert sum(item["good_requests"] for item in goodput_timeline) == 1
    error_timeline = result.summary["error_timeline"]
    assert sum(item["failed_requests"] for item in error_timeline) == 1
    assert error_timeline[-1]["cumulative_failures"] == 1


@pytest.mark.asyncio
async def test_aiperf_parser_preserves_streaming_metrics_and_ignores_warmup(simulation_root):
    task = task_record(simulation_root, "a1e0f001")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    stdout = artifacts / "stdout.log"
    stderr = artifacts / "stderr.log"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    (artifacts / "profile_export_aiperf.json").write_text(
        json.dumps(
            {
                "schema_version": "1.4",
                "request_count": {"unit": "requests", "avg": 1},
                "error_request_count": {"unit": "requests", "avg": 1},
                "benchmark_duration": {"unit": "sec", "avg": 2},
                "request_throughput": {"unit": "requests/sec", "avg": 1},
                "output_token_throughput": {"unit": "tokens/sec", "avg": 5},
                "input_token_throughput": {"unit": "tokens/sec", "avg": 10},
                "total_token_throughput": {"unit": "tokens/sec", "avg": 15},
                "request_error_rate": {"unit": "%", "avg": 50},
                "effective_concurrency": {"unit": "requests", "avg": 1.5},
                "total_isl": {"unit": "tokens", "avg": 20},
                "total_osl": {"unit": "tokens", "avg": 10},
                "request_latency": {"unit": "ms", "avg": 120, "p50": 110, "count": 1},
                "time_to_first_token": {
                    "unit": "ms",
                    "avg": 30,
                    "p50": 28,
                    "p99": 40,
                    "count": 1,
                },
                "inter_token_latency": {
                    "unit": "ms",
                    "avg": 8,
                    "p50": 7,
                    "p99": 11,
                    "count": 1,
                },
                "replay_sched_lag_p50": {"unit": "ms", "avg": 2},
                "replay_sched_lag_p90": {"unit": "ms", "avg": 4},
                "replay_sched_lag_p99": {"unit": "ms", "avg": 6},
            }
        ),
        encoding="utf-8",
    )
    records = [
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 1_000_000_000,
                "request_end_ns": 1_120_000_000,
            },
            "metrics": {
                "request_latency": {"value": 120, "unit": "ms"},
                "time_to_first_token": {"value": 30, "unit": "ms"},
                "inter_token_latency": {"value": 8, "unit": "ms"},
                "input_sequence_length": {"value": 20, "unit": "tokens"},
                "output_sequence_length": {"value": 10, "unit": "tokens"},
                "osl_mismatch_diff_pct": {"value": -90, "unit": "%"},
            },
        },
        {
            "metadata": {
                "benchmark_phase": "profiling",
                "request_start_ns": 2_000_000_000,
                "request_end_ns": 2_050_000_000,
            },
            "metrics": {},
            "error": {"code": 503, "message": "unavailable"},
        },
        {
            "metadata": {
                "benchmark_phase": "warmup",
                "request_start_ns": 500_000_000,
                "request_end_ns": 600_000_000,
            },
            "metrics": {
                "request_latency": {"value": 100, "unit": "ms"},
                "input_sequence_length": {"value": 999, "unit": "tokens"},
                "output_sequence_length": {"value": 999, "unit": "tokens"},
            },
        },
    ]
    (artifacts / "profile_export.jsonl").write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )

    now = datetime.now(UTC)
    result = await AIPerfBackend().parse(
        task,
        CommandResult(0, now, now, stdout, stderr),
        "0.12.0",
    )
    assert isinstance(result, SimulationResult)
    assert all(isinstance(artifact, SimulationArtifact) for artifact in result.artifacts)
    summary = result.summary
    assert summary["total_requests"] == 2
    assert summary["successful_requests"] == 1
    assert summary["failed_requests"] == 1
    assert summary["total_input_tokens"] == 20
    assert summary["total_output_tokens"] == 10
    assert summary["ttft"]["mean_ms"] == 30
    assert summary["ttft"]["count"] == 1
    assert summary["tpot"]["p99_ms"] == 11
    assert summary["input_throughput_tps"] == 10
    assert summary["total_throughput_tps"] == 15
    assert summary["effective_concurrency"] == 1.5
    assert summary["drift"] == {"p50_ms": 2, "p90_ms": 4, "p99_ms": 6}
    assert sum(item["arrived_requests"] for item in summary["completion_timeline"]) == 2
    assert summary["tpot_heatmap"]["sequence_length_source"] == "requested"
    assert summary["tpot_heatmap"]["cells"][0]["sequence_length_start"] == 100
    assert summary["tpot_heatmap"]["cells"][0]["sequence_length_end"] == 101
    assert result.warnings == []


@pytest.mark.asyncio
async def test_aiperf_parser_reports_non_streaming_metrics_as_unavailable(simulation_root):
    task = task_record(simulation_root, "a1e0f002")
    update_simulation(task, stream=False)
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    stdout = artifacts / "stdout.log"
    stderr = artifacts / "stderr.log"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    (artifacts / "profile_export_aiperf.json").write_text(
        json.dumps(
            {
                "schema_version": "1.4",
                "request_count": {"unit": "requests", "avg": 7},
                "error_request_count": {"unit": "requests", "avg": 2},
                "total_isl": {"unit": "tokens", "avg": 70},
                "total_osl": {"unit": "tokens", "avg": 21},
                "request_latency": {"unit": "ms", "avg": 100},
            }
        ),
        encoding="utf-8",
    )

    now = datetime.now(UTC)
    result = await AIPerfBackend().parse(
        task,
        CommandResult(0, now, now, stdout, stderr),
        "0.12.0",
    )
    summary = result.summary
    assert summary["total_requests"] == 9
    assert summary["successful_requests"] == 7
    assert summary["failed_requests"] == 2
    assert summary["total_input_tokens"] == 70
    assert summary["total_output_tokens"] == 21
    assert summary["ttft"] is None
    assert summary["tpot"] is None
    assert "non-streaming responses" in result.warnings[0]


def test_aiperf_timeout_record_is_reported_as_failure(simulation_root):
    task = task_record(simulation_root, "a1e0f003")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    (artifacts / "profile_export.jsonl").write_text(
        json.dumps(
            {
                "metadata": {
                    "benchmark_phase": "profiling",
                    "request_start_ns": 1_000_000_000,
                    "request_end_ns": 2_000_000_000,
                },
                "metrics": {},
                "timeout": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    records = AIPerfBackend().artifact_records(task)

    assert len(records) == 1
    assert records[0].successful is False
    assert records[0].issue_failed is True
    assert records[0].error == "Request timed out"


def test_live_completion_timeline_ignores_partial_record(simulation_root):
    task = task_record(simulation_root, "1234abcd", "running")
    update_simulation(task, backend="trace-replayer")
    artifacts = Path(task.task_dir) / "artifacts" / "trace-replayer"
    artifacts.mkdir(parents=True)
    (artifacts / "requests.jsonl").write_text(
        '{"status":"200","s_time":"100","e_time":"500"}\n{"status":"200","s_time":"750"',
        encoding="utf-8",
    )

    timeline = live_completion_timeline_from_artifacts(task)

    assert sum(point["arrived_requests"] for point in timeline) == 1
    assert sum(point["completed_requests"] for point in timeline) == 1


def test_live_summary_reports_current_metrics(simulation_root):
    task = task_record(simulation_root, "1234abcd", "running")
    update_simulation(task, backend="trace-replayer")
    artifacts = Path(task.task_dir) / "artifacts" / "trace-replayer"
    artifacts.mkdir(parents=True)
    (artifacts / "requests.jsonl").write_text(
        '{"input_length":"128","output_length":"20","status":"200","s_time":"0",'
        '"e_time":"1000","first_token_time":"100","avg_time_between_tokens":"10"}\n'
        '{"input_length":"128","output_length":"40","status":"200","s_time":"1000",'
        '"e_time":"2000","first_token_time":"300","avg_time_between_tokens":"30"}\n',
        encoding="utf-8",
    )

    summary = live_summary_from_artifacts(task)

    assert summary["throughput_rps"] == 1
    assert summary["throughput_tps"] == 30
    assert summary["total_requests"] == 2
    assert summary["successful_requests"] == 2
    assert summary["failed_requests"] == 0
    assert summary["success_rate"] == 100
    assert summary["ttft"]["mean_ms"] == 200
    assert summary["tpot"]["mean_ms"] == 20


def test_aiperf_live_summary_counts_partial_error_output_tokens(simulation_root):
    task = task_record(simulation_root, "1234abcd", "running")
    artifacts = Path(task.task_dir) / "artifacts" / "aiperf"
    artifacts.mkdir(parents=True)
    (artifacts / "profile_export.jsonl").write_text(
        json.dumps(
            {
                "metadata": {
                    "benchmark_phase": "profiling",
                    "request_start_ns": 1_000_000_000,
                    "request_end_ns": 2_000_000_000,
                },
                "metrics": {"output_sequence_length": {"value": 7}},
                "error": {"code": 500, "message": "partial response"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = live_summary_from_artifacts(task)

    assert summary["failed_requests"] == 1
    assert summary["throughput_tps"] == 7


def test_live_summary_derives_trace_tpot_from_total_time(simulation_root):
    task = task_record(simulation_root, "1234abcd", "running")
    update_simulation(task, backend="trace-replayer")
    artifacts = Path(task.task_dir) / "artifacts" / "trace-replayer"
    artifacts.mkdir(parents=True)
    (artifacts / "requests.jsonl").write_text(
        '{"output_length":"20","status":"200","s_time":"0","e_time":"1000",'
        '"first_token_time":"100","total_time":"200"}\n',
        encoding="utf-8",
    )

    summary = live_summary_from_artifacts(task)

    assert summary["tpot"]["mean_ms"] == 10


def test_live_summary_reports_error_timeline(simulation_root):
    task = task_record(simulation_root, "1234abcd", "running")
    update_simulation(task, backend="trace-replayer")
    artifacts = Path(task.task_dir) / "artifacts" / "trace-replayer"
    artifacts.mkdir(parents=True)
    (artifacts / "requests.jsonl").write_text(
        '{"output_length":"20","status":"200","s_time":"0","e_time":"1000"}\n'
        '{"output_length":"0","status":"429","s_time":"1000","e_time":"2000"}\n',
        encoding="utf-8",
    )

    summary = live_summary_from_artifacts(task)

    assert summary["throughput_timeline"]
    assert sum(point["failed_requests"] for point in summary["error_timeline"]) == 1
    assert summary["error_timeline"][-1]["cumulative_failures"] == 1
    assert summary["error_timeline"][-1]["error_rate_percent"] == 50


def test_status_code_breakdown_supports_arbitrary_codes(simulation_root):
    task = task_record(simulation_root, "1234abcd", "completed")
    update_simulation(task, backend="trace-replayer")
    artifacts = Path(task.task_dir) / "artifacts" / "trace-replayer"
    artifacts.mkdir(parents=True)
    (artifacts / "requests.jsonl").write_text(
        '{"status":"200"}\n'
        '{"status":"401"}\n'
        '{"status":"418","error_message":"teapot rejected request",'
        '"s_time":"1000","e_time":"1250","input_length":"12","output_length":"0"}\n'
        '{"status":"429"}\n'
        '{"status":"500"}\n'
        '{"status":"503"}\n'
        '{"error":"connection reset"}\n',
        encoding="utf-8",
    )

    breakdown = status_code_breakdown_from_artifacts(task)
    records = TraceReplayerBackend().artifact_records(task)

    assert all(isinstance(record, ArtifactRecord) for record in records)
    assert records[2].status_code == 418
    assert records[2].error == "teapot rejected request"
    assert [item["status_code"] for item in breakdown] == [401, 418, 429, 500, 503, None]
    assert all(item["count"] == 1 for item in breakdown)
    assert sum(item["percentage"] for item in breakdown) == pytest.approx(100)
    issues = response_code_issues_from_artifacts(task, 418)
    assert issues.total == 1
    assert issues.items[0].model_dump(mode="json") == {
        "request_number": 3,
        "request_id": None,
        "status_code": 418,
        "error": "teapot rejected request",
        "started_at_seconds": 1,
        "latency_ms": 250,
        "input_tokens": 12,
        "output_tokens": 0,
    }
    assert response_code_issues_from_artifacts(task, 418, offset=1, limit=1).items == []

    page = SimulationResponseCodeIssuePage(
        task_id=task.id,
        status_code=418,
        items=issues.items,
        total=issues.total,
        offset=0,
        limit=100,
    )
    assert page.model_dump(mode="json", by_alias=True)["issues"][0]["status_code"] == 418

    asyncio.run(save_task(task))
    with TestClient(app) as client:
        response = client.get(f"/api/simulation/tasks/{task.id}/response-code-issues?status_code=418")
    assert response.status_code == 200
    assert response.json() == {
        "task_id": task.id,
        "status_code": 418,
        "issues": [
            {
                "request_number": 3,
                "request_id": None,
                "status_code": 418,
                "error": "teapot rejected request",
                "started_at_seconds": 1.0,
                "latency_ms": 250.0,
                "input_tokens": 12.0,
                "output_tokens": 0.0,
            }
        ],
        "total": 1,
        "offset": 0,
        "limit": 100,
    }


def test_resolve_task_endpoint_url_uses_incluster_endpoint_for_deployment_mode(monkeypatch):
    import types

    context = types.SimpleNamespace(
        endpoint="http://vllm.my-namespace.svc:8000",
        namespace="my-namespace",
        cluster_id="cluster-123",
        display_name="qwen3-0.6b",
    )
    monkeypatch.setattr("llm_d_bench.deploy.executions.get_execution_context", lambda execution_id: context)

    resolved = asyncio.run(
        simulation_service._resolve_task_endpoint_url(
            endpoint_mode="deployment",
            endpoint_url="http://127.0.0.1:18042",
            endpoint_deployment_execution_id="exec-1",
        )
    )

    assert resolved.url == "http://vllm.my-namespace.svc:8000"
    assert resolved.namespace == "my-namespace"
    assert resolved.cluster_id == "cluster-123"
    assert resolved.deployment_name == "qwen3-0.6b"


def test_resolve_task_endpoint_url_prefers_cluster_gateway(monkeypatch):
    import types

    context = types.SimpleNamespace(
        endpoint="http://optimized-baseline-epp.my-namespace.svc:80",
        namespace="my-namespace",
        cluster_id="cluster-123",
        display_name="qwen3-0.6b",
        uses_shared_gateway=True,
    )
    monkeypatch.setattr("llm_d_bench.deploy.executions.get_execution_context", lambda execution_id: context)

    async def gateway(_cluster_id):
        return "http://10.0.0.5:30012"

    monkeypatch.setattr(simulation_service, "_cluster_gateway_endpoint", gateway)

    resolved = asyncio.run(
        simulation_service._resolve_task_endpoint_url(
            endpoint_mode="deployment",
            endpoint_url="http://127.0.0.1:18042",
            endpoint_deployment_execution_id="exec-1",
        )
    )

    assert resolved.url == "http://10.0.0.5:30012"

    context.uses_shared_gateway = False
    direct = asyncio.run(
        simulation_service._resolve_task_endpoint_url(
            endpoint_mode="deployment",
            endpoint_url="http://127.0.0.1:18042",
            endpoint_deployment_execution_id="exec-1",
        )
    )
    assert direct.url == "http://optimized-baseline-epp.my-namespace.svc:80"


def test_resolve_task_endpoint_url_rejects_deployment_without_endpoint(monkeypatch):
    import types

    context = types.SimpleNamespace(endpoint=None, namespace="my-namespace", cluster_id=None, display_name=None)
    monkeypatch.setattr("llm_d_bench.deploy.executions.get_execution_context", lambda execution_id: context)

    with pytest.raises(SimulationConfigurationError):
        asyncio.run(
            simulation_service._resolve_task_endpoint_url(
                endpoint_mode="deployment",
                endpoint_url="http://127.0.0.1:18042",
                endpoint_deployment_execution_id="exec-1",
            )
        )


def test_resolve_task_endpoint_url_rejects_incompatible_deployment_guide(monkeypatch):
    import types

    context = types.SimpleNamespace(
        endpoint="http://pd-disaggregation-epp.my-namespace.svc:80",
        namespace="my-namespace",
        cluster_id="cluster-123",
        display_name="test1",
        guide="pd-disaggregation",
    )
    monkeypatch.setattr("llm_d_bench.deploy.executions.get_execution_context", lambda execution_id: context)

    with pytest.raises(SimulationConfigurationError, match="pd-disaggregation"):
        asyncio.run(
            simulation_service._resolve_task_endpoint_url(
                endpoint_mode="deployment",
                endpoint_url="http://127.0.0.1:18042",
                endpoint_deployment_execution_id="exec-1",
                backend_name="trace-replayer",
                backend_capabilities={"incompatible_deployment_guides": ["pd-disaggregation"]},
            )
        )

    # A guide not on the backend's incompatibility list is unaffected.
    resolved = asyncio.run(
        simulation_service._resolve_task_endpoint_url(
            endpoint_mode="deployment",
            endpoint_url="http://127.0.0.1:18042",
            endpoint_deployment_execution_id="exec-1",
            backend_name="aiperf",
            backend_capabilities={"incompatible_deployment_guides": []},
        )
    )
    assert resolved.url == "http://pd-disaggregation-epp.my-namespace.svc:80"


def test_resolve_incluster_target_only_applies_to_deployment_mode_with_namespace():
    task = task_record(Path("/tmp"), "1234abcd")
    assert resolve_incluster_target(task) is None

    task.endpoint_mode = "deployment"
    assert resolve_incluster_target(task) is None  # no namespace resolved yet

    task.endpoint_namespace = "my-namespace"
    task.endpoint_cluster_id = "cluster-123"
    assert resolve_incluster_target(task) == ("my-namespace", "cluster-123")


def test_incluster_image_defaults_aiperf_to_official_image(monkeypatch):
    from llm_d_bench.simulation import incluster

    monkeypatch.delenv("SIMULATION_INCLUSTER_IMAGE", raising=False)
    monkeypatch.delenv("SIMULATION_AIPERF_INCLUSTER_IMAGE", raising=False)
    assert incluster.incluster_image("aiperf") == "nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0"
    assert incluster.incluster_image("trace-replayer") == "python:3.12-slim"
    assert incluster.incluster_image(None) == "python:3.12-slim"


def test_incluster_image_aiperf_specific_override(monkeypatch):
    from llm_d_bench.simulation import incluster

    monkeypatch.delenv("SIMULATION_INCLUSTER_IMAGE", raising=False)
    monkeypatch.setenv("SIMULATION_AIPERF_INCLUSTER_IMAGE", "example.com/custom/aiperf:test")
    assert incluster.incluster_image("aiperf") == "example.com/custom/aiperf:test"
    assert incluster.incluster_image("trace-replayer") == "python:3.12-slim"


def test_incluster_image_global_override_wins_over_aiperf_default(monkeypatch):
    from llm_d_bench.simulation import incluster

    monkeypatch.setenv("SIMULATION_INCLUSTER_IMAGE", "example.com/custom/all:test")
    monkeypatch.setenv("SIMULATION_AIPERF_INCLUSTER_IMAGE", "example.com/custom/aiperf:test")
    assert incluster.incluster_image("aiperf") == "example.com/custom/all:test"
    assert incluster.incluster_image("trace-replayer") == "example.com/custom/all:test"


def test_pod_manifest_runs_as_root_for_arbitrary_path_staging():
    from llm_d_bench.simulation.incluster import _pod_manifest

    manifest = _pod_manifest("pod-name", "ns", "nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0", 60)
    container = manifest["spec"]["containers"][0]
    assert container["securityContext"] == {"runAsUser": 0, "runAsGroup": 0}


def test_pod_exec_command_aiperf_invokes_bare_name_without_copy():
    from llm_d_bench.simulation.incluster import _pod_exec_command

    command = _pod_exec_command("/home/user/.venv/bin/aiperf", ["profile"], executable_copied=False)
    assert "aiperf profile" in command
    # No self-install needed when it's already baked into the dedicated image,
    # but the fallback check must still be present for image overrides.
    assert "command -v aiperf" in command
    assert "/home/user/.venv/bin/aiperf" not in command


@pytest.mark.asyncio
async def test_command_backend_runs_in_pod_for_deployment_endpoint(simulation_root, monkeypatch):
    task = task_record(simulation_root, "1234abcd")
    task.endpoint_mode = "deployment"
    task.endpoint_namespace = "my-namespace"
    task.endpoint_cluster_id = "cluster-123"
    backend = get_backend("aiperf")

    async def validate(_task):
        return None

    async def prepare(_context):
        return sys.executable

    async def command(_task):
        return BackendCommand(args=("--version",), timeout_seconds=10)

    async def parse(_task, _result, _version):
        return SimulationResult(
            run_id=_task.id,
            backend="aiperf",
            backend_version=_version,
            summary={},
            artifacts=[],
            per_request=[],
            backend_metrics={},
            warnings=[],
        )

    calls = {}

    async def fake_execute_command_in_pod(**kwargs):
        calls.update(kwargs)
        root = Path(task.task_dir)
        root.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        return CommandResult(0, now, now, root / "stdout.log", root / "stderr.log")

    async def fail_execute_command(*_args, **_kwargs):
        raise AssertionError("Local execute_command should not run for deployment-mode tasks")

    monkeypatch.setattr(backend, "validate", validate)
    monkeypatch.setattr(backend, "prepare_executable", prepare)
    monkeypatch.setattr(backend, "command", command)
    monkeypatch.setattr(backend, "parse", parse)
    monkeypatch.setattr(backend, "expected_request_count", lambda _task: 1)
    monkeypatch.setattr("llm_d_bench.simulation.backends.base.execute_command_in_pod", fake_execute_command_in_pod)
    monkeypatch.setattr("llm_d_bench.simulation.backends.base.execute_command", fail_execute_command)

    context = RunContext(asyncio.Event(), lambda *_args: None, lambda *_args: None)
    await backend.run(task, context)

    assert calls["namespace"] == "my-namespace"
    assert calls["cluster_id"] == "cluster-123"
    assert calls["task_dir"] == Path(task.task_dir)


@pytest.mark.asyncio
async def test_trace_replayer_runs_in_pod_for_deployment_endpoint(simulation_root, monkeypatch):
    task = task_record(simulation_root, "1234abcd")
    task.endpoint_mode = "deployment"
    task.endpoint_namespace = "serving-ns"
    task.endpoint_cluster_id = "cluster-456"
    update_simulation(task, backend="trace-replayer")
    backend = get_backend("trace-replayer")

    async def validate(_task):
        return None

    async def prepare(_context):
        return sys.executable

    async def command(_task):
        return BackendCommand(args=("--version",), timeout_seconds=10)

    async def parse(_task, _result, _version):
        return SimulationResult(
            run_id=_task.id,
            backend="trace-replayer",
            backend_version=_version,
            summary={},
            artifacts=[],
            per_request=[],
            backend_metrics={},
            warnings=[],
        )

    calls = {}

    async def fake_execute_command_in_pod(**kwargs):
        calls.update(kwargs)
        root = Path(task.task_dir)
        root.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        return CommandResult(0, now, now, root / "stdout.log", root / "stderr.log")

    monkeypatch.setattr(backend, "validate", validate)
    monkeypatch.setattr(backend, "prepare_executable", prepare)
    monkeypatch.setattr(backend, "command", command)
    monkeypatch.setattr(backend, "parse", parse)
    monkeypatch.setattr(backend, "expected_request_count", lambda _task: 1)
    monkeypatch.setattr("llm_d_bench.simulation.backends.base.execute_command_in_pod", fake_execute_command_in_pod)

    context = RunContext(asyncio.Event(), lambda *_args: None, lambda *_args: None)
    await backend.run(task, context)

    assert calls["namespace"] == "serving-ns"
    assert calls["cluster_id"] == "cluster-456"
    assert calls["task_dir"] == Path(task.task_dir)


def test_simulation_storage_uses_unified_roots(tmp_path, monkeypatch):
    for name in (
        "SIMULATION_TASK_ROOT",
        "TRACE_REPLAY_DATA_DIR",
        "SIMULATION_BACKEND_CACHE_DIR",
        "SIMULATION_TOKENIZER_CACHE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    assert simulation_service.task_root() == tmp_path / "data/artifacts/simulations"
    assert BaseTrace.trace_root() == tmp_path / "data/datasets"
    assert TraceReplayerBackend._tokenizer_cache_root() == tmp_path / "cache/tokenizers"
    assert TraceReplayerBackend.managed_executable_path().is_relative_to(tmp_path / "cache/backends")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_terminal_simulation_registers_actual_files(simulation_root, status):
    import hashlib

    task = task_record(simulation_root, "aabbcc01", status)
    directory = Path(task.task_dir)
    payload = directory / "artifacts/aiperf/stdout.log"
    payload.parent.mkdir(parents=True)
    payload.write_text("real backend output\n", encoding="utf-8")
    await save_task(task)
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["owner_type"] == "simulation"
    assert manifest["owner_id"] == task.id
    assert manifest["status"] == status
    record = next(item for item in manifest["files"] if item["path"] == "artifacts/aiperf/stdout.log")
    assert record["sha256"] == hashlib.sha256(payload.read_bytes()).hexdigest()
    assert record["uri"] == f"lens-artifact://simulation/{task.id}/artifacts/aiperf/stdout.log"
    assert task.artifact_uri == f"lens-artifact://simulation/{task.id}/"
    assert manifest["source_version"]["backend"] == "aiperf"
    assert manifest["source_version"]["dataset"] == "mooncake-arxiv"


@pytest.mark.asyncio
async def test_dataset_download_has_distinct_manifest_and_provenance(simulation_root):
    result = await BaseTrace.download("baseten-synthetic")
    source = Path(result["path"])
    manifest_path = source.with_name(source.name + ".manifest.json")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["owner_type"] == "dataset"
    assert manifest["owner_id"] == "baseten-synthetic"
    assert {item["path"] for item in manifest["files"]} == {
        source.name,
        source.name + ".metadata.json",
        BasetenTrace(source).timeline_index_path.name,
    }
    assert not (source.parent / "manifest.json").exists()
    assert result["artifact_uri"] == f"lens-artifact://dataset/baseten-synthetic/{source.name}"
    assert manifest["source_version"]["source"] == "generated"


@pytest.mark.asyncio
async def test_simulation_failure_retains_log_truncation_and_configuration_ids(simulation_root, monkeypatch):
    from types import SimpleNamespace

    task = task_record(simulation_root, "aabbcc02", "queued")
    monkeypatch.setenv("SIMULATION_MAX_TASK_LOG_LINES", "1")

    async def resolve(**kwargs):
        return simulation_service.ResolvedTaskEndpoint(url=task.endpoint_url, configuration_ids=("configuration-123",))

    async def fail(task, context):
        context.log("first line")
        context.log("last line")
        raise SimulationConfigurationError("backend failed")

    monkeypatch.setattr(simulation_service, "_resolve_task_endpoint_url", resolve)
    monkeypatch.setattr(
        simulation_service,
        "get_backend",
        lambda name: SimpleNamespace(
            run=fail,
            descriptor=lambda: SimpleNamespace(version="test-version"),
        ),
    )
    await simulation_service._run_task(task)
    manifest = json.loads((Path(task.task_dir) / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["configuration_ids"] == ["configuration-123"]
    assert manifest["source_version"]["backend_version"] == "test-version"
    assert manifest["truncated"] is True
    assert next(item for item in manifest["files"] if item["path"] == "task.json")["truncated"] is True
    assert len(task.logs) == 1


def test_deployment_execution_context_exposes_configuration_artifacts(monkeypatch):
    from types import SimpleNamespace

    from llm_d_bench.deploy import executions

    execution = SimpleNamespace(
        execution_id="execution-123",
        status="ready",
        namespace="default",
        provenance={},
        endpoint=None,
        forwarded_endpoint=None,
        artifact=SimpleNamespace(configuration_artifact_ids=["configuration-123"]),
    )
    monkeypatch.setattr(
        executions,
        "serving_metadata",
        lambda *args: dict.fromkeys(
            (
                "name",
                "model_name",
                "backend",
                "guide",
                "replicas",
                "tensor_parallel_size",
                "image",
                "max_model_len",
                "gpu_memory_utilization",
                "enable_prefix_caching",
                "max_num_seqs",
                "max_num_batched_tokens",
                "storage_type",
                "storage_volume_id",
                "mount_path",
                "pvc_name",
                "custom_parameters",
            )
        ),
    )
    context = executions._context(None, None, execution)
    assert context.configuration_artifact_ids == ("configuration-123",)


@pytest.mark.asyncio
async def test_terminal_manifest_failure_does_not_publish_artifact_refs(simulation_root, monkeypatch):
    task = task_record(simulation_root, "aabbcc03")
    await save_task(task)
    assert task.artifact_uri

    def fail_registration(*args, **kwargs):
        raise OSError("manifest disk failure")

    monkeypatch.setattr(simulation_service, "register_artifacts", fail_registration)
    with pytest.raises(OSError, match="manifest disk failure"):
        await save_task(task)
    payload = json.loads((Path(task.task_dir) / "task.json").read_text())
    assert not payload.get("artifact_uri")
    assert task.artifact_uri is None
    assert "manifest disk failure" in payload["artifact_error"]
    assert not (Path(task.task_dir) / "manifest.json").exists()
    loaded = await load_task(task.id)
    assert loaded.artifact_uri is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_interrupted_simulation_native_files_marked_incomplete(simulation_root, status):
    task = task_record(simulation_root, "aabbcc04", status)
    output = Path(task.task_dir) / "artifacts/requests.jsonl"
    output.parent.mkdir(parents=True)
    output.write_text('{"partial":')
    await save_task(task)
    manifest = json.loads((Path(task.task_dir) / "manifest.json").read_text())
    assert manifest["truncated"]
    assert next(item for item in manifest["files"] if item["path"] == "artifacts/requests.jsonl")["truncated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_fails", [False, True])
async def test_cancelled_pod_collects_evidence_before_cleanup(simulation_root, monkeypatch, copy_fails):
    from unittest.mock import AsyncMock

    from llm_d_bench.simulation import incluster

    events = []
    pod = incluster._PodHandle("test", "default", None)
    monkeypatch.setattr(incluster, "_create_pod", AsyncMock(return_value=pod))
    monkeypatch.setattr(incluster, "_wait_pod_running", AsyncMock(side_effect=SimulationCancelledError("cancelled")))

    async def collect(*args):
        events.append("collect")
        if copy_fails:
            raise OSError("copy failed")
        return True

    async def cleanup(*args):
        events.append("delete")

    monkeypatch.setattr(incluster, "_copy_out_of_pod", collect)
    monkeypatch.setattr(incluster, "_delete_pod", cleanup)
    context = RunContext(asyncio.Event(), lambda message: None, lambda *args: None)
    with pytest.raises(SimulationCancelledError):
        await incluster.execute_command_in_pod(
            namespace="default",
            cluster_id=None,
            task_dir=simulation_root,
            executable="aiperf",
            args=[],
            artifact_dir=simulation_root / "artifacts",
            timeout_seconds=30,
            context=context,
        )
    assert events == ["collect", "delete"]
    if copy_fails:
        assert context.artifacts_incomplete


@pytest.mark.asyncio
async def test_loading_changed_task_does_not_advertise_stale_manifest(simulation_root):
    task = task_record(simulation_root, "aabbcc05")
    await save_task(task)
    loaded = await load_task(task.id)
    assert loaded.artifact_uri == task.artifact_uri
    path = Path(task.task_dir) / "task.json"
    payload = json.loads(path.read_text())
    payload["description"] = "new record before manifest publication"
    path.write_text(json.dumps(payload))
    loaded = await load_task(task.id)
    assert loaded.artifact_uri is None


@pytest.mark.asyncio
async def test_tolerated_host_timeout_marks_artifacts_incomplete(simulation_root):
    context = RunContext(asyncio.Event(), lambda message: None, lambda *args: None)
    result = await execute_command(
        sys.executable,
        ["-c", "import time; time.sleep(5)"],
        simulation_root / "timeout-artifacts",
        0.05,
        context,
        tolerate_timeout=True,
    )
    assert result.timed_out
    assert context.artifacts_incomplete


@pytest.mark.asyncio
async def test_create_task_resolves_a_model_service_group_to_its_healthy_member(simulation_root, monkeypatch):
    """A Simulation task may target a published Model Service instead of picking a
    deployment execution directly -- only a Model Service is continuously
    health-probed, so this is the only existing-endpoint path with live backend
    liveness (see llm_d_bench/model_service/resolution.py).
    """
    import types

    trace = simulation_root / "datasets" / "mooncake_trace.jsonl"
    trace.write_text('{"timestamp":0,"input_length":1,"output_length":1,"hash_ids":[]}\n', encoding="utf-8")
    monkeypatch.setattr(simulation_service, "_schedule_task", lambda _task: None)

    group = types.SimpleNamespace(id="msg-1", name="qwen3-prod", status="active", selection_policy="random")
    member = types.SimpleNamespace(execution_id="exec-1")

    class _FakeGroups:
        def get(self, group_id):
            return group if group_id == "msg-1" else None

    class _FakeModelService:
        groups = _FakeGroups()

        def authorized_members(self, group, principal):  # noqa: ARG002
            return [member]

    monkeypatch.setattr("llm_d_bench.model_service.service.default_service", lambda: _FakeModelService())
    context = types.SimpleNamespace(
        endpoint="http://vllm.my-namespace.svc:8000",
        namespace="my-namespace",
        cluster_id="cluster-123",
        display_name="qwen3-0.6b",
    )
    monkeypatch.setattr("llm_d_bench.deploy.executions.get_execution_context", lambda execution_id: context)

    request = SimulationTaskCreateRequest(
        scenario="chat",
        backend="aiperf",
        endpoint_mode="external",
        endpoint_url="http://model-service.internal",
        model_name="placeholder",
        model_service_group_id="msg-1",
        api_key="lens-mk-x",
        trace_dataset="mooncake-arxiv",
        trace_path=trace.name,
    )

    task = await create_task(request)

    assert task.endpoint_mode == "deployment"
    assert task.endpoint_deployment_execution_id == "exec-1"
    assert task.model_name == "qwen3-prod"
    assert task.endpoint_url == "http://vllm.my-namespace.svc:8000"


def test_model_service_group_id_requires_an_api_key():
    with pytest.raises(ValidationError, match="api_key"):
        SimulationTaskCreateRequest(
            scenario="chat",
            backend="aiperf",
            endpoint_url="http://model-service.internal",
            model_name="placeholder",
            model_service_group_id="msg-1",
            trace_dataset="mooncake-arxiv",
            trace_path="trace.jsonl",
        )
