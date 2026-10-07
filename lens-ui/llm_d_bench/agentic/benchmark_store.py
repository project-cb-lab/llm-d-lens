"""Durable, versioned benchmark evidence for Agentic planning.

Backed by ``BenchmarkRecordDao`` (SQLAlchemy) -- see design doc
section 5.4.12. ``base_dir`` is accepted but ignored: it exists only so the
many existing call sites that construct ``JsonBenchmarkRecordStore(tmp_path)``
(to get an isolated store per test) keep working unchanged; isolation is now
provided by the per-test database engine (see the repo root
``conftest.py``) instead of a per-instance directory.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from llm_d_bench.db.dao.agentic_benchmark_record import BenchmarkRecordDao

from .benchmark_evidence import BenchmarkQuery, BenchmarkRecord


class JsonBenchmarkRecordStore:
    """Persist immutable BenchmarkRecord payloads emitted from completed Evaluate runs."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        del base_dir  # unused -- see module docstring
        self._dao = BenchmarkRecordDao()

    def save(self, record: BenchmarkRecord) -> BenchmarkRecord:
        return self._dao.save(record)

    def list(self) -> list[BenchmarkRecord]:
        return self._dao.list()

    def find_compatible(
        self,
        query: BenchmarkQuery,
        *,
        observed_after: datetime | None = None,
    ) -> list[BenchmarkRecord]:
        """Return DAO-filtered records for retriever fine scoring."""
        return self._dao.find_compatible(
            accelerator=query.hardware.accelerator,
            backend=query.runtime.backend,
            quantization=query.model.quantization,
            topology=query.candidate.topology,
            minimum_vram_per_gpu_gib=query.hardware.vram_per_gpu_gib,
            observed_after=observed_after,
        )

    def find_seed_candidates(
        self,
        query: BenchmarkQuery,
        *,
        observed_after: datetime | None = None,
    ) -> list[BenchmarkRecord]:
        """Return cross-topology history for validated candidate seed discovery."""
        return self._dao.find_seed_candidates(
            accelerator=query.hardware.accelerator,
            backend=query.runtime.backend,
            quantization=query.model.quantization,
            minimum_vram_per_gpu_gib=query.hardware.vram_per_gpu_gib,
            observed_after=observed_after,
        )
