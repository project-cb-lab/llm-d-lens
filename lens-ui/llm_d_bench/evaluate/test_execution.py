"""The shared case step preserves workload, failure evidence and cancellation."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from llm_d_bench.evaluate.execution import case_benchmark_request, execute_case_benchmark
from llm_d_bench.evaluate.models import BenchmarkSpec


@pytest.mark.parametrize("baseline", [False, True])
def test_case_request_preserves_benchmark_and_runtime(baseline):
    spec = BenchmarkSpec(parallelism=2, warmup_requests=0, harness_memory_gib=16).model_dump()
    case = {"benchmark": spec, "sla_targets": {"ttft_ms": 123}}
    workflow = {"runtime": {"http_proxy": "http://proxy:8080", "no_proxy": ".svc"}}
    before = deepcopy((workflow, case))
    request = case_benchmark_request(
        workflow, case, "execution", "guides/optimized-baseline", use_baseline_endpoint=baseline
    )
    assert request.model_dump(include=set(BenchmarkSpec.model_fields)) == spec
    assert request.deployment_execution_id == "execution"
    assert request.http_proxy == "http://proxy:8080"
    assert request.no_proxy == ".svc"
    assert request.use_baseline_endpoint is baseline
    assert (workflow, case) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
async def test_case_step_records_result_before_propagating_failure(status):
    workflow, case, saved = {}, {"id": "case"}, []
    result = {
        "status": status,
        "metrics": {"throughput_tps": 0},
        "resource_snapshot": {"pods": []},
        "error": None if status == "succeeded" else "stopped",
    }

    async def create(_request):
        return {"id": "benchmark"}

    async def wait(run_id):
        assert run_id == "benchmark"
        assert saved[0]["evaluation_run_id"] == run_id
        return result

    def link(_workflow, _case, benchmark):
        workflow["linked"] = benchmark["id"]

    def save(_workflow):
        saved.append(deepcopy(case))

    def enrich(record):
        record["enriched"] = True

    operation = execute_case_benchmark(
        workflow,
        case,
        object(),
        create_run=create,
        link_benchmark=link,
        save=save,
        wait_for_benchmark=wait,
        enrich=enrich,
        now=lambda: "finished",
    )
    if status == "succeeded":
        await operation
    else:
        with pytest.raises(ValueError, match="stopped"):
            await operation
    assert workflow["linked"] == "benchmark"
    assert saved[-1]["status"] == status
    assert saved[-1]["metrics"]["throughput_tps"] == 0
    assert saved[-1]["resource_snapshot"] == {"pods": []}
    assert saved[-1]["enriched"] is True


@pytest.mark.asyncio
async def test_case_step_does_not_swallow_task_cancellation():
    case = {"id": "case"}
    with pytest.raises(asyncio.CancelledError):
        await execute_case_benchmark(
            {},
            case,
            object(),
            create_run=AsyncMock(return_value={"id": "run"}),
            link_benchmark=lambda *_: None,
            save=lambda *_: None,
            wait_for_benchmark=AsyncMock(side_effect=asyncio.CancelledError),
            enrich=lambda *_: None,
            now=lambda: "never",
        )
    assert case == {"id": "case", "evaluation_run_id": "run"}
