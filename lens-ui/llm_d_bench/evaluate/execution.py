"""Shared Evaluation case submission/result steps; deployment ownership stays with orchestration."""

from .models import EvaluateRunRequest


def case_benchmark_request(workflow, case, execution_id, specification_file, *, use_baseline_endpoint=False):
    benchmark_spec = case.get("benchmark") or case["spec"]["benchmark"]
    return EvaluateRunRequest(
        deployment_execution_id=execution_id,
        sla_targets=case.get("sla_targets") or {},
        specification_file=specification_file,
        harness=benchmark_spec["harness"],
        workload=benchmark_spec["workload"],
        parallelism=benchmark_spec["parallelism"],
        wait_timeout_seconds=benchmark_spec["wait_timeout_seconds"],
        harness_memory_gib=benchmark_spec.get("harness_memory_gib", 32),
        accelerator_profile=benchmark_spec.get("accelerator_profile"),
        storage_class_name=benchmark_spec.get("storage_class_name"),
        http_proxy=workflow["runtime"].get("http_proxy", ""),
        https_proxy=workflow["runtime"].get("https_proxy", ""),
        no_proxy=workflow["runtime"].get("no_proxy", ""),
        benchmark_source=workflow.get("benchmark_source"),
        matrix=benchmark_spec.get("matrix") or [],
        concurrency_stages=benchmark_spec.get("concurrency_stages") or [],
        warmup_requests=benchmark_spec.get("warmup_requests", 2),
        shared_prefix=benchmark_spec.get("shared_prefix"),
        workload_yaml=benchmark_spec.get("workload_yaml"),
        use_baseline_endpoint=use_baseline_endpoint,
    )


async def execute_case_benchmark(
    workflow, case, request, *, create_run, link_benchmark, save, wait_for_benchmark, enrich, now
):
    benchmark = await create_run(request)
    link_benchmark(workflow, case, benchmark)
    case["evaluation_run_id"] = benchmark["id"]
    save(workflow)
    result = await wait_for_benchmark(benchmark["id"])
    case.update(
        status=result["status"],
        metrics=result.get("metrics", {}),
        matrix_results=result.get("matrix_results", []),
        rate_stage_results=result.get("rate_stage_results", []),
        result_output=result.get("output"),
        error=result.get("error"),
        finished_at=now(),
    )
    enrich(case)
    case["resource_snapshot"] = result.get("resource_snapshot")
    save(workflow)
    if result["status"] != "succeeded":
        raise ValueError(result.get("error") or f"case {case['id']} failed")
