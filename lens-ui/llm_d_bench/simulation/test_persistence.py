from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llm_d_bench.simulation.models import (
    SimulationArtifact,
    SimulationConfig,
    SimulationDataset,
    SimulationPrompt,
    SimulationResult,
    SimulationTask,
    SimulationTrace,
)
from llm_d_bench.simulation.service import load_task, reset_service_state, save_task


@pytest.fixture
def simulation_root(tmp_path, monkeypatch):
    traces = tmp_path / "traces"
    data = tmp_path / "data"
    traces.mkdir()
    monkeypatch.setenv("TRACE_REPLAY_DATA_DIR", str(traces))
    monkeypatch.setenv("LENS_DATA_DIR", str(data))
    reset_service_state()
    yield tmp_path
    reset_service_state()
    shutil.rmtree(tmp_path, ignore_errors=True)


def task_record(root: Path, task_id: str) -> SimulationTask:
    from llm_d_bench.simulation.service import task_root

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return SimulationTask(
        id=task_id,
        name=f"task {task_id}",
        description="",
        scenario="chat",
        status="completed",
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
            dataset=SimulationDataset(name="mooncake-arxiv", scenario="chat", tokenizer=None),
            trace=SimulationTrace(
                path="mooncake_trace.jsonl",
                format="mooncake_trace",
                fixed_schedule=True,
                timeout_seconds=900,
                synthesis_speedup_ratio=1,
            ),
        ),
        task_dir=str(task_root() / task_id),
        progress_percent=100,
        progress_message="completed",
        logs=["done"],
        result=None,
        error_message=None,
        created_at=now,
        started_at=None,
        execution_started_at=None,
        completed_at=now,
        owner_pid=None,
        owner_instance_id=None,
    )


@pytest.mark.asyncio
async def test_persistence_round_trip_rehydrates_per_request_and_result_extra(simulation_root):
    task = task_record(simulation_root, "1234abcd")
    task.result = SimulationResult.model_validate(
        {
            "run_id": task.id,
            "backend": "aiperf",
            "backend_version": "0.12.0",
            "summary": {"throughput_tps": 7},
            "artifacts": [
                SimulationArtifact(
                    kind="summary",
                    path=str(Path(task.task_dir) / "artifacts" / "summary.json"),
                    media_type="application/json",
                )
            ],
            "per_request": [{"request_id": "request-1", "ttft_ms": 1.5}],
            "backend_metrics": {"custom_backend_metric": True},
            "warnings": [],
            "historical_result_extension": {"kept": True},
        }
    )

    await save_task(task)
    loaded = await load_task(task.id)

    assert loaded is not None
    assert loaded.result is not None
    assert loaded.result.per_request[0].request_id == "request-1"
    assert loaded.result.per_request[0].ttft_ms == 1.5
    assert loaded.result.model_extra["historical_result_extension"] == {"kept": True}
