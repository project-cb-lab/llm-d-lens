"""DAO backing ``llm_d_bench.agentic.benchmark_store`` -- see design doc section 5.4.12.

Append-only: ``save`` is idempotent (a no-op if the id already exists),
matching ``JsonBenchmarkRecordStore.save``'s "write once" semantics -- there
is deliberately no update/delete method on this repository.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from llm_d_bench.agentic.benchmark_evidence import BenchmarkRecord
from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.agentic_benchmark_record import BenchmarkRecordRow


class BenchmarkRecordDao(BaseDao):
    def save(self, record: BenchmarkRecord) -> BenchmarkRecord:
        with self._transaction() as session:
            existing = session.get(BenchmarkRecordRow, record.benchmark_id)
            if existing is not None:
                return record
            row = BenchmarkRecordRow.from_dto(record)
            session.add(row)
            session.flush()
            return row.to_dto()

    def list(self) -> list[BenchmarkRecord]:
        with self._read_only() as session:
            stmt = select(BenchmarkRecordRow).order_by(BenchmarkRecordRow.timestamp, BenchmarkRecordRow.benchmark_id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def find_compatible(
        self,
        *,
        accelerator: str,
        backend: str,
        quantization: str,
        topology: str,
        minimum_vram_per_gpu_gib: float,
        observed_after: datetime | None = None,
    ) -> list[BenchmarkRecord]:
        """Return records eligible for deterministic retriever fine scoring.

        These predicates exactly match the retriever's hard compatibility rules.
        Continuous similarity inputs and outcome remain unfiltered so Agentic can
        compute weights, confidence, and negative evidence consistently.
        """
        with self._read_only() as session:
            stmt = select(BenchmarkRecordRow).where(
                BenchmarkRecordRow.hardware_accelerator == accelerator,
                BenchmarkRecordRow.runtime_backend == backend,
                BenchmarkRecordRow.model_quantization == quantization,
                BenchmarkRecordRow.configuration_topology == topology,
                BenchmarkRecordRow.hardware_vram_per_gpu_gib >= minimum_vram_per_gpu_gib,
            )
            if observed_after is not None:
                stmt = stmt.where(BenchmarkRecordRow.timestamp >= observed_after)
            stmt = stmt.order_by(BenchmarkRecordRow.timestamp, BenchmarkRecordRow.benchmark_id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def find_seed_candidates(
        self,
        *,
        accelerator: str,
        backend: str,
        quantization: str,
        minimum_vram_per_gpu_gib: float,
        observed_after: datetime | None = None,
    ) -> list[BenchmarkRecord]:
        """Return hard-compatible history across topologies for seed discovery.

        The caller must still aggregate each configuration through
        ``BenchmarkEvidenceRetriever`` and validate every resulting proposal.
        """
        with self._read_only() as session:
            stmt = select(BenchmarkRecordRow).where(
                BenchmarkRecordRow.hardware_accelerator == accelerator,
                BenchmarkRecordRow.runtime_backend == backend,
                BenchmarkRecordRow.model_quantization == quantization,
                BenchmarkRecordRow.hardware_vram_per_gpu_gib >= minimum_vram_per_gpu_gib,
            )
            if observed_after is not None:
                stmt = stmt.where(BenchmarkRecordRow.timestamp >= observed_after)
            stmt = stmt.order_by(BenchmarkRecordRow.timestamp, BenchmarkRecordRow.benchmark_id)
            return [row.to_dto() for row in session.scalars(stmt)]
