"""Finite argv compatibility adapter: unsupported syntax has no SDK effects."""

import json

import pytest

from llm_d_bench.utils.test_kubernetes_reads import cluster_api as cluster_api


@pytest.mark.asyncio
async def test_create_single_manifest_stdin_uses_object_namespace(cluster_api):
    from llm_d_bench.utils.kubernetes_commands import execute_sdk_command

    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "credential", "namespace": "models"},
        "stringData": {"key": "fixture-value"},
    }
    cluster_api.response = body
    result = await execute_sdk_command(
        ["kubectl", "create", "-f", "-"], stdin_data=json.dumps(body), kubeconfig=str(cluster_api.config)
    )
    assert result.returncode == 0
    assert cluster_api.methods == ["POST"]
    assert cluster_api.calls[-1][0] == "/api/v1/namespaces/models/secrets"
    assert "fixture-value" not in result.stdout


@pytest.mark.asyncio
async def test_get_adapter_preserves_command_result_and_field_selector(cluster_api):
    from llm_d_bench.utils.kubernetes_commands import execute_sdk_command

    argv = ["kubectl", "get", "pods", "-A", "--field-selector=status.phase=Running", "-o", "json"]
    result = await execute_sdk_command(argv, kubeconfig=str(cluster_api.config))
    assert result.returncode == 0
    assert json.loads(result.stdout)["items"][0]["metadata"]["resourceVersion"] == "42"
    assert cluster_api.calls[-1][1]["fieldSelector"] == "status.phase=Running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        ["apply", "-f", "manifest.yaml"],
        ["exec", "pod", "--", "sh"],
        ["get", "pods", "--watch", "-o", "json"],
        ["delete", "pods", "--force"],
        ["get", "pods", "-o", "jsonpath={.items}"],
        ["patch", "node", "x", "--dry-run=client"],
    ],
)
async def test_unsupported_syntax_selects_cli_before_request(cluster_api, args):
    from llm_d_bench.utils.kubernetes_commands import execute_sdk_command

    assert await execute_sdk_command(["kubectl", *args], kubeconfig=str(cluster_api.config)) is None
    assert cluster_api.calls == []


@pytest.mark.asyncio
async def test_api_error_is_nonzero_result_without_body_disclosure(cluster_api):
    from llm_d_bench.utils.kubernetes_commands import execute_sdk_command

    cluster_api.status = 403
    cluster_api.response = {"message": "secret-value"}
    result = await execute_sdk_command(["kubectl", "get", "nodes", "-o", "json"], kubeconfig=str(cluster_api.config))
    assert result.returncode == 1
    assert "403" in result.stderr and "secret-value" not in result.stderr


@pytest.mark.asyncio
async def test_multiple_types_merge_lists_and_version_wraps_server_version(cluster_api):
    from llm_d_bench.utils.kubernetes_commands import execute_sdk_command

    result = await execute_sdk_command(
        ["kubectl", "get", "nodes,namespaces", "-o", "json"], kubeconfig=str(cluster_api.config)
    )
    assert len(json.loads(result.stdout)["items"]) == 2
    cluster_api.response = {"gitVersion": "v1.35.0"}
    result = await execute_sdk_command(["kubectl", "version", "-o", "json"], kubeconfig=str(cluster_api.config))
    assert json.loads(result.stdout) == {"serverVersion": {"gitVersion": "v1.35.0"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        ["label", "node/x", "key=value", "--overwrite"],
        ["label", "node", "x", "key=value", "--overwrite"],
    ],
)
async def test_labels_combined_and_separate_names(monkeypatch, args):
    from llm_d_bench.utils import kubernetes_commands as commands

    calls = []

    async def label(resource, name, labels, **kwargs):
        calls.append((resource, name, labels, kwargs))
        return {"metadata": {"name": name}}

    monkeypatch.setattr(commands.mutations, "label_sdk_resource", label)
    result = await commands.execute_sdk_command(["kubectl", *args], kubeconfig="cluster")
    assert result.returncode == 0
    assert calls[0][:3] == ("node", "x", {"key": "value"})
    assert calls[0][3]["overwrite"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        ["delete", "pods", "named", "-l", "app=other"],
        ["delete", "pods/named", "--all"],
        ["delete", "pods/named", "-A"],
        ["delete", "pods", "--all", "-l", "app=other"],
        ["delete", "pods", "--all", "--wait=true", "--wait=false"],
        ["get", "pods/named", "another", "-o", "json"],
        ["get", "pods", "named", "-l", "app=other", "-o", "json"],
        ["get", "pods,services", "named", "-o", "json"],
        ["get", "--raw=/version", "--selector=ignored"],
        ["get", "pods", "-o", "json", "--request-timeout=5s"],
        ["logs", "job/name"],
        ["delete", "pods", "--all", "--timeout=garbage"],
        ["patch", "pods,services", "name", "-p", "{}"],
    ],
)
async def test_ambiguous_or_unimplemented_options_fall_back_before_request(cluster_api, args):
    from llm_d_bench.utils.kubernetes_commands import execute_sdk_command

    assert await execute_sdk_command(["kubectl", *args], kubeconfig=str(cluster_api.config)) is None
    assert cluster_api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("verb,status,reason", [("create", 409, "AlreadyExists"), ("delete", 404, "NotFound")])
async def test_safe_canonical_reason_preserves_existing_caller_checks(monkeypatch, verb, status, reason):
    from kubernetes.aio.client.exceptions import ApiException

    from llm_d_bench.utils import kubernetes_commands as commands

    async def fail(*args, **kwargs):
        error = ApiException(status=status, reason="untrusted secret")
        error.body = json.dumps({"reason": reason, "message": "private-value"})
        raise error

    monkeypatch.setattr(commands.mutations, "create_sdk_resource", fail)
    monkeypatch.setattr(commands.mutations, "delete_sdk_resource", fail)
    result = await commands.execute_sdk_command(["kubectl", verb, "namespace", "example"])
    assert result.returncode == 1
    assert reason in result.stderr
    assert "private-value" not in result.stderr and "untrusted secret" not in result.stderr


@pytest.mark.asyncio
async def test_named_delete_preserves_namespace_and_wait_options(monkeypatch):
    from llm_d_bench.utils import kubernetes_commands as commands

    calls = []

    async def delete(resource, name, **kwargs):
        calls.append((resource, name, kwargs))
        return {}

    monkeypatch.setattr(commands.mutations, "delete_sdk_resource", delete)
    result = await commands.execute_sdk_command(
        [
            "kubectl",
            "delete",
            "job/name",
            "-n",
            "work",
            "--ignore-not-found=true",
            "--wait=false",
            "--cascade=foreground",
            "--timeout=1m30s",
        ]
    )
    assert result.returncode == 0
    assert calls[0][:2] == ("job", "name")
    assert calls[0][2] == {
        "namespace": "work",
        "kubeconfig": None,
        "timeout": 30,
        "ignore_not_found": True,
        "wait": False,
        "propagation_policy": "Foreground",
    }


@pytest.mark.parametrize(
    ("backend", "legacy", "reads", "writes"),
    [
        (None, None, True, True),
        ("cli", None, False, False),
        ("sdk", None, True, True),
        (None, "sdk", True, False),
        (None, "cli", False, False),
        ("sdk", "cli", True, True),
        ("cli", "sdk", False, False),
        (" SDK ", "invalid", True, True),
    ],
)
def test_backend_selection_precedence(monkeypatch, backend, legacy, reads, writes):
    from llm_d_bench.utils.kubernetes_commands import sdk_enabled, sdk_writes_enabled

    for key, value in [("PRISM_KUBERNETES_BACKEND", backend), ("PRISM_KUBERNETES_READ_BACKEND", legacy)]:
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)
    assert sdk_enabled() is reads
    assert sdk_writes_enabled() is writes
