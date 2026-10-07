"""Raw SDK contracts using the isolated local API fixture."""

import pytest

from llm_d_bench.utils.test_kubernetes_reads import cluster_api as cluster_api


@pytest.mark.asyncio
async def test_custom_resource_discovery_uses_served_preferred_version(cluster_api):
    from llm_d_bench.utils.kubernetes_api import query_resource

    responses = {
        "/apis": {
            "groups": [{"name": "resource.k8s.io", "preferredVersion": {"groupVersion": "resource.k8s.io/v1beta1"}}]
        },
        "/apis/resource.k8s.io/v1beta1": {
            "resources": [
                {"name": "resourceslices", "kind": "ResourceSlice", "namespaced": False, "verbs": ["get", "list"]},
            ]
        },
        "/apis/resource.k8s.io/v1beta1/resourceslices": {"items": [{"metadata": {"name": "gpu"}}]},
    }
    cluster_api.response = lambda req: responses[req.path]
    result = await query_resource("resourceslices.resource.k8s.io", kubeconfig=str(cluster_api.config))
    assert result["items"] == [{"metadata": {"name": "gpu"}}]


@pytest.mark.asyncio
async def test_named_get_retains_fields_and_selector_list_retains_resource_version(cluster_api):
    from llm_d_bench.utils.kubernetes_api import query_resource

    cluster_api.response = {"kind": "Secret", "data": {"key": "eA=="}}
    assert await query_resource("secrets", name="credential", namespace="ns", kubeconfig=str(cluster_api.config)) == {
        "kind": "Secret",
        "data": {"key": "eA=="},
    }
    assert cluster_api.calls[-1][0] == "/api/v1/namespaces/ns/secrets/credential"
    cluster_api.response = {"metadata": {"resourceVersion": "123"}, "items": []}
    result = await query_resource(
        "pods", field_selector="status.phase=Running", all_namespaces=True, kubeconfig=str(cluster_api.config)
    )
    assert result["metadata"]["resourceVersion"] == "123"
    assert cluster_api.calls[-1][1]["fieldSelector"] == "status.phase=Running"


@pytest.mark.asyncio
async def test_strict_query_does_not_swallow_forbidden(cluster_api):
    from kubernetes.aio.client.exceptions import ApiException

    from llm_d_bench.utils.kubernetes_api import query_resource

    cluster_api.status = 403
    with pytest.raises(ApiException) as error:
        await query_resource("nodes", kubeconfig=str(cluster_api.config))
    assert error.value.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alias,path",
    [
        ("ingress", "/apis/networking.k8s.io/v1/namespaces/selected/ingresses"),
        ("networkpolicy", "/apis/networking.k8s.io/v1/namespaces/selected/networkpolicies"),
        ("storageclass", "/apis/storage.k8s.io/v1/storageclasses"),
        ("deployment.apps", "/apis/apps/v1/namespaces/selected/deployments"),
    ],
)
async def test_builtin_singular_aliases(cluster_api, alias, path):
    from llm_d_bench.utils.kubernetes_api import query_resource

    await query_resource(alias, kubeconfig=str(cluster_api.config))
    assert cluster_api.calls[-1][0] == path


@pytest.mark.asyncio
async def test_core_version_is_not_an_api_group(cluster_api):
    from kubernetes.aio.client.exceptions import ApiException

    from llm_d_bench.utils.kubernetes_api import query_resource

    cluster_api.response = {"groups": []}
    with pytest.raises(ApiException) as error:
        await query_resource("nodes.v1", kubeconfig=str(cluster_api.config))
    assert error.value.status == 404
    assert all(path != "/api/v1/nodes" for path, _, _ in cluster_api.calls)


@pytest.mark.asyncio
async def test_later_page_forbidden_never_returns_partial_data(cluster_api):
    from kubernetes.aio.client.exceptions import ApiException

    from llm_d_bench.utils.kubernetes_api import query_resource

    def response(request):
        if request.query.get("continue"):
            cluster_api.status = 403
            return {"message": "forbidden"}
        return {"items": [{"metadata": {"name": "first"}}], "metadata": {"continue": "next"}}

    cluster_api.response = response
    with pytest.raises(ApiException) as error:
        await query_resource("pods", kubeconfig=str(cluster_api.config))
    assert error.value.status == 403
    assert len(cluster_api.calls) == 2


@pytest.mark.asyncio
async def test_raw_labels_unknown_fields_and_list_metadata_survive_pages(cluster_api):
    from llm_d_bench.utils.kubernetes_api import query_resource

    first = {"metadata": {"labels": {"example.com/key": "v"}, "name": "first"}, "futureField": {"flag": True}}
    cluster_api.response = lambda request: (
        {"apiVersion": "v1", "kind": "PodList", "items": [{"metadata": {"name": "second"}}]}
        if request.query.get("continue")
        else {
            "apiVersion": "v1",
            "kind": "PodList",
            "items": [first],
            "metadata": {"continue": "next", "resourceVersion": "42", "remainingItemCount": 1},
        }
    )
    result = await query_resource("pods", kubeconfig=str(cluster_api.config))
    assert result["items"] == [
        {**first, "kind": "Pod", "apiVersion": "v1"},
        {"metadata": {"name": "second"}, "kind": "Pod", "apiVersion": "v1"},
    ]
    assert result["metadata"] == {"resourceVersion": "42", "continue": ""}
    assert result["kind"] == "PodList"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "list_kind,version,kind",
    [
        ("PodList", "v1", "Pod"),
        ("DeploymentList", "apps/v1", "Deployment"),
        ("PersistentVolumeClaimList", "v1", "PersistentVolumeClaim"),
        ("PersistentVolumeList", "v1", "PersistentVolume"),
        ("ResourceSliceList", "resource.k8s.io/v1beta1", "ResourceSlice"),
    ],
)
async def test_list_items_receive_missing_type_metadata(cluster_api, list_kind, version, kind):
    from llm_d_bench.utils.kubernetes_api import query_resource

    cluster_api.response = {
        "kind": list_kind,
        "apiVersion": version,
        "items": [{"metadata": {"name": "first"}}, {"kind": "ExistingKind", "apiVersion": "example.com/v2"}],
    }
    result = await query_resource("pods", kubeconfig=str(cluster_api.config))
    assert result["items"][0] == {"metadata": {"name": "first"}, "kind": kind, "apiVersion": version}
    assert result["items"][1] == {"kind": "ExistingKind", "apiVersion": "example.com/v2"}


@pytest.mark.asyncio
async def test_generic_list_does_not_invent_item_types(cluster_api):
    from llm_d_bench.utils.kubernetes_api import query_resource

    cluster_api.response = {"kind": "List", "apiVersion": "v1", "items": [{"metadata": {"name": "mixed"}}]}
    result = await query_resource("pods", kubeconfig=str(cluster_api.config))
    assert result["items"] == [{"metadata": {"name": "mixed"}}]


@pytest.mark.asyncio
async def test_pooled_session_revalidates_token_and_deleted_configuration(cluster_api):
    from kubernetes.aio.config.config_exception import ConfigException

    from llm_d_bench.utils.kubernetes_api import api_session, query_resource
    from llm_d_bench.utils.kubernetes_pool import close_pool, start_pool

    await start_pool()
    try:
        async with api_session(str(cluster_api.config)) as (first, _):
            pass
        async with api_session(str(cluster_api.config)) as (second, _):
            assert second is first
        await query_resource("nodes", kubeconfig=str(cluster_api.config))
        cluster_api.write_config(credential="rotated")
        async with api_session(str(cluster_api.config)) as (rotated, _):
            assert rotated is not first
        await query_resource("nodes", kubeconfig=str(cluster_api.config))
        assert [call[2] for call in cluster_api.calls] == ["Bearer test-a", "Bearer rotated"]
        cluster_api.config.unlink()
        with pytest.raises(ConfigException):
            await query_resource("nodes", kubeconfig=str(cluster_api.config))
        assert len(cluster_api.calls) == 2
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_static_client_certificate_over_local_mutual_tls(tmp_path):
    import ipaddress
    import ssl
    from datetime import UTC, datetime, timedelta

    import yaml
    from aiohttp import web

    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from llm_d_bench.utils.kubernetes_api import query_resource
    from llm_d_bench.utils.kubernetes_pool import close_pool, start_pool

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local-fixture")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    key_path.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    context.load_verify_locations(cafile=cert_path)
    context.verify_mode = ssl.CERT_REQUIRED
    peers = []

    async def handle(request):
        peers.append(request.transport.get_extra_info("peercert"))
        return web.json_response({"items": [{"metadata": {"name": "tls-node"}}]})

    app = web.Application()
    app.router.add_get("/api/v1/nodes", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
        host, port = runner.addresses[0]
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "current-context": "local",
                    "clusters": [
                        {
                            "name": "local",
                            "cluster": {"server": f"https://{host}:{port}", "certificate-authority": str(cert_path)},
                        }
                    ],
                    "users": [
                        {"name": "reader", "user": {"client-certificate": str(cert_path), "client-key": str(key_path)}}
                    ],
                    "contexts": [{"name": "local", "context": {"cluster": "local", "user": "reader"}}],
                }
            )
        )
        await start_pool()
        try:
            for _ in range(2):
                result = await query_resource("nodes", kubeconfig=str(config))
                assert result["items"][0]["metadata"]["name"] == "tls-node"
        finally:
            await close_pool()
        assert len(peers) == 2 and all(peers)
        assert cert_path.exists() and key_path.exists()
    finally:
        await runner.cleanup()
