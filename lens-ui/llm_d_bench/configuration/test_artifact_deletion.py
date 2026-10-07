"""Configuration artifact catalog deletion tests."""

from datetime import UTC, datetime

from llm_d_bench.configuration import service
from llm_d_bench.configuration.models import ConfigurationArtifactRecord, ConfigurationFile
from llm_d_bench.db.dao.configuration_artifact import ConfigurationArtifactDao
from llm_d_bench.deploy.contracts import DeployableConfiguration
from llm_d_bench.utils.artifacts import configuration_checksum


def test_delete_configuration_artifact_removes_record_and_manifest(monkeypatch, tmp_path):
    output = tmp_path / "configurations"
    output.mkdir()
    monkeypatch.setattr(service, "CONFIGURATION_OUTPUT_DIR", output)
    artifact_id = "00000000-0000-4000-8000-000000000001"
    content = {"model": {"name": "Qwen/Qwen3-8B"}}
    configuration = DeployableConfiguration(
        type="baseline",
        provider_ref="optimized-baseline",
        format="manifest",
        content=content,
        checksum=configuration_checksum(content),
        provenance={},
    )
    artifact = ConfigurationArtifactRecord(
        artifact_id=artifact_id,
        deployable_configuration=configuration,
        configuration_file=ConfigurationFile(
            type="baseline",
            file_name="candidate.yaml",
            format="manifest",
            path="/configs/candidate.yaml",
            size=4,
            sha256="0" * 64,
        ),
        created_at=datetime.now(UTC),
    )
    # The record now lives in the database (see design doc section 5.4.5);
    # only the rendered manifest file itself remains on disk, so seed the
    # artifact via the repository instead of writing a JSON file by hand.
    ConfigurationArtifactDao().create(artifact)
    (output / "candidate.yaml").write_text("test", encoding="utf-8")

    assert service.delete_configuration_artifact(artifact_id) is True
    assert service.get_configuration_artifact(artifact_id) is None
    assert not (output / "candidate.yaml").exists()
    assert service.delete_configuration_artifact(artifact_id) is False
