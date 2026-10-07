"""``agentic_benchmark_records`` table -- see design doc section 5.4.12.

Backs ``llm_d_bench.agentic.benchmark_evidence.BenchmarkRecord``. Append-only:
``save()`` is idempotent (skips if the id already exists), matching the old
file-store's "write once, ignore afterward" semantics -- there is
deliberately no update/delete method. The six nested one-to-one sub-models
(``model``/``hardware``/``runtime``/``workload``/``configuration``/
``metrics``) are expanded into prefixed columns per rule 2 (section 5.2).

``evaluate_run_id`` is a *soft reference* (not a real foreign key) per the
design doc section 5.3: it's derived by parsing the ``"evaluate:<uuid>"``
convention out of ``benchmark_id`` (see ``llm_d_bench/agentic/facts.py``),
and evaluate runs may be deleted/pruned independently of this permanent
audit trail, so it must never cascade or block on a live ``evaluate_runs``
row.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from llm_d_bench.agentic.benchmark_evidence import BenchmarkRecord
from llm_d_bench.db.base import ArrayVariant, Base, DeclarativeDtoMixin, UTCDateTime

_EVALUATE_BENCHMARK_ID_PREFIX = "evaluate:"


class BenchmarkRecordRow(Base, DeclarativeDtoMixin):
    __tablename__ = "agentic_benchmark_records"

    dto_type = BenchmarkRecord
    column_map = {
        "timestamp": "timestamp",
        "outcome": "outcome",
        "model_model_id": "model.model_id",
        "model_family": "model.family",
        "model_architecture": "model.architecture",
        "model_parameter_count_b": "model.parameter_count_b",
        "model_active_parameter_count_b": "model.active_parameter_count_b",
        "model_quantization": "model.quantization",
        "model_tokenizer_id": "model.tokenizer_id",
        "model_revision": "model.revision",
        "hardware_accelerator": "hardware.accelerator",
        "hardware_vram_per_gpu_gib": "hardware.vram_per_gpu_gib",
        "hardware_gpu_count": "hardware.gpu_count",
        "runtime_backend": "runtime.backend",
        "runtime_backend_version": "runtime.backend_version",
        "runtime_accelerator_runtime": "runtime.accelerator_runtime",
        "runtime_driver_version": "runtime.driver_version",
        "runtime_container_digest": "runtime.container_digest",
        "runtime_git_revision": "runtime.git_revision",
        "workload_mean_input_tokens": "workload.mean_input_tokens",
        "workload_p95_input_tokens": "workload.p95_input_tokens",
        "workload_mean_output_tokens": "workload.mean_output_tokens",
        "workload_concurrency": "workload.concurrency",
        "workload_request_rate": "workload.request_rate",
        "workload_shared_prefix_ratio": "workload.shared_prefix_ratio",
        "workload_prefill_fraction": "workload.prefill_fraction",
        "configuration_topology": "configuration.topology",
        "configuration_decode_tp": "configuration.decode_tp",
        "configuration_decode_replicas": "configuration.decode_replicas",
        "configuration_prefill_tp": "configuration.prefill_tp",
        "configuration_prefill_replicas": "configuration.prefill_replicas",
        "metrics_ttft_p95_ms": "metrics.ttft_p95_ms",
        "metrics_tpot_p95_ms": "metrics.tpot_p95_ms",
        "metrics_e2e_p95_ms": "metrics.e2e_p95_ms",
        "metrics_throughput_tokens_per_s": "metrics.throughput_tokens_per_s",
        "metrics_error_rate": "metrics.error_rate",
        "failure_reason": "failure_reason",
    }

    benchmark_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    model_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    model_architecture: Mapped[str] = mapped_column(String(16), nullable=False)
    model_parameter_count_b: Mapped[float] = mapped_column(Float, nullable=False)
    model_active_parameter_count_b: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_quantization: Mapped[str] = mapped_column(String(64), nullable=False)
    model_tokenizer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    model_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hardware_accelerator: Mapped[str] = mapped_column(String(64), nullable=False)
    hardware_vram_per_gpu_gib: Mapped[float] = mapped_column(Float, nullable=False)
    hardware_gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_backend: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_backend_version: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_accelerator_runtime: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_driver_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runtime_container_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    runtime_git_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workload_mean_input_tokens: Mapped[float] = mapped_column(Float, nullable=False)
    workload_p95_input_tokens: Mapped[float] = mapped_column(Float, nullable=False)
    workload_mean_output_tokens: Mapped[float] = mapped_column(Float, nullable=False)
    workload_concurrency: Mapped[float] = mapped_column(Float, nullable=False)
    workload_request_rate: Mapped[float] = mapped_column(Float, nullable=False)
    workload_shared_prefix_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    workload_prefill_fraction: Mapped[float] = mapped_column(Float, nullable=False)
    configuration_topology: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_decode_tp: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_decode_replicas: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_prefill_tp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    configuration_prefill_replicas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    configuration_optimizations: Mapped[list[str]] = mapped_column(ArrayVariant, nullable=False, default=list)
    metrics_ttft_p95_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics_tpot_p95_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics_e2e_p95_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics_throughput_tokens_per_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics_error_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluate_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    __table_args__ = (
        Index("ix_agentic_benchmark_records_timestamp", "timestamp"),
        Index("ix_agentic_benchmark_records_model_model_id", "model_model_id"),
    )

    @classmethod
    def from_dto(cls, dto: BenchmarkRecord, **extra_columns: object) -> BenchmarkRecordRow:
        values: dict[str, object] = {col: _dget(dto, path) for col, path in cls.column_map.items()}
        values["benchmark_id"] = dto.benchmark_id
        values["configuration_optimizations"] = list(dto.configuration.optimizations)
        values["evaluate_run_id"] = _evaluate_run_id(dto.benchmark_id)
        values.update(extra_columns)
        return cls(**values)

    def to_dto(self) -> BenchmarkRecord:
        values: dict[str, object] = {}
        for col, path in self.column_map.items():
            _dset(values, path, getattr(self, col))
        values["benchmark_id"] = self.benchmark_id
        values["configuration"]["optimizations"] = tuple(self.configuration_optimizations)
        return BenchmarkRecord.model_validate(values)


def _dget(dto: BenchmarkRecord, path: str) -> object:
    obj: object = dto
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _dset(root: dict[str, object], path: str, value: object) -> None:
    *parents, leaf = path.split(".")
    node = root
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value


def _evaluate_run_id(benchmark_id: str) -> str | None:
    if benchmark_id.startswith(_EVALUATE_BENCHMARK_ID_PREFIX):
        return benchmark_id[len(_EVALUATE_BENCHMARK_ID_PREFIX) :]
    return None
