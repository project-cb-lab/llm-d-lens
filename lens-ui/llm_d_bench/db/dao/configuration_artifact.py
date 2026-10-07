"""DAO backing ``llm_d_bench.configuration.service``'s artifact persistence.

See design doc section 5.4.5. This table is append-mostly (artifacts are
immutable once published -- ``save_configuration`` only ever creates new
rows), so there's no ``save``/update method, matching the module's existing
``get``/``list``/``delete`` surface.
"""

from __future__ import annotations

from sqlalchemy import select

from llm_d_bench.configuration.models import ConfigurationArtifactRecord
from llm_d_bench.db.dao.base import BaseDao
from llm_d_bench.db.models.configuration_artifact import ConfigurationArtifactRow


class ConfigurationArtifactDaoError(Exception):
    """Raised for configuration-artifact-repository failures that aren't already a domain error."""


class ConfigurationArtifactDao(BaseDao):
    def create(self, artifact: ConfigurationArtifactRecord) -> ConfigurationArtifactRecord:
        with self._transaction() as session:
            if session.get(ConfigurationArtifactRow, artifact.artifact_id) is not None:
                raise ConfigurationArtifactDaoError(f"configuration artifact already exists: {artifact.artifact_id}")
            row = ConfigurationArtifactRow.from_dto(artifact)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, artifact_id: str) -> ConfigurationArtifactRecord | None:
        with self._read_only() as session:
            row = session.get(ConfigurationArtifactRow, artifact_id)
            return row.to_dto() if row else None

    def list(self) -> list[ConfigurationArtifactRecord]:
        with self._read_only() as session:
            stmt = select(ConfigurationArtifactRow).order_by(ConfigurationArtifactRow.created_at.desc())
            return [row.to_dto() for row in session.scalars(stmt)]

    def delete(self, artifact_id: str) -> None:
        with self._transaction() as session:
            row = session.get(ConfigurationArtifactRow, artifact_id)
            if row is not None:
                session.delete(row)
