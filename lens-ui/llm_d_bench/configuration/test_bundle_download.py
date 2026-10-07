import importlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

router = importlib.import_module("llm_d_bench.configuration.router")


@pytest.mark.asyncio
async def test_manifest_download_uses_saved_content_when_filename_is_reused(monkeypatch, tmp_path):
    service = importlib.import_module("llm_d_bench.configuration.service")
    (tmp_path / "shared.yaml").write_text("kind: WrongDeployment")
    monkeypatch.setattr(service, "CONFIGURATION_OUTPUT_DIR", tmp_path)
    artifact = SimpleNamespace(
        deployable_configuration=SimpleNamespace(content={"officialGuide": {"renderedManifest": "kind: Deployment"}}),
        configuration_file=SimpleNamespace(file_name="shared.yaml"),
    )
    monkeypatch.setattr(router, "get_configuration_artifact", lambda _: artifact)
    response = await router.download_artifact_manifest("original-id")
    assert response.body == b"kind: Deployment"


@pytest.mark.asyncio
async def test_bundle_download_contains_immutable_model_and_router_inputs(monkeypatch):
    bundle = {
        "helm": {
            "chart": "oci://router",
            "version": "v1",
            "values": [{"name": "router-effective.yaml", "content": "router: {}"}],
        },
        "resources": [{"name": "render.yaml", "content": "kind: Service"}],
    }
    content = {"officialGuide": {"renderedManifest": "kind: Deployment", "deploymentBundle": bundle}}
    artifact = SimpleNamespace(
        deployable_configuration=SimpleNamespace(content=content),
        model_dump_json=lambda **_: json.dumps({"configuration": content}),
    )
    monkeypatch.setattr(router, "get_configuration_artifact", lambda _: artifact)
    response = await router.download_artifact_bundle("saved-id")
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert archive.read("modelserver.yaml") == b"kind: Deployment"
        assert archive.read("router-effective.yaml") == b"router: {}"
        assert archive.read("render.yaml") == b"kind: Service"
        assert json.loads(archive.read("deployment-bundle.json"))["helm"]["version"] == "v1"


@pytest.mark.asyncio
async def test_legacy_modelserver_only_artifact_cannot_claim_a_complete_bundle(monkeypatch):
    artifact = SimpleNamespace(
        deployable_configuration=SimpleNamespace(content={"officialGuide": {"renderedManifest": "kind: Deployment"}})
    )
    monkeypatch.setattr(router, "get_configuration_artifact", lambda _: artifact)
    with pytest.raises(router.HTTPException) as error:
        await router.download_artifact_bundle("legacy-id")
    assert error.value.status_code == 409
