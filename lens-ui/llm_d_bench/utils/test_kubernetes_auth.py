"""Exec authentication tests use real local helpers and no cluster."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from kubernetes.aio.config.config_exception import ConfigException

from llm_d_bench.utils import kubernetes_auth as auth


@pytest.fixture
def configuration(tmp_path):
    def write(script=None, *, execution=None, user=None):
        if script is not None:
            helper = tmp_path / "helper.py"
            helper.write_text(script)
            execution = {
                "apiVersion": "client.authentication.k8s.io/v1",
                "interactiveMode": "Never",
                "command": sys.executable,
                "args": [str(helper)],
            } | (execution or {})
        document = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "selected",
            "contexts": [
                {"name": "selected", "context": {"cluster": "cluster", "user": "user", "namespace": "chosen"}}
            ],
            "clusters": [{"name": "cluster", "cluster": {"server": "https://example.invalid"}}],
            "users": [{"name": "user", "user": user if user is not None else {"exec": execution}}],
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(document))
        return str(path)

    return write


def response(status, **changes):
    return {"apiVersion": "client.authentication.k8s.io/v1", "kind": "ExecCredential", "status": status} | changes


def output(document):
    return "print(" + repr(json.dumps(document)) + ")\n"


@pytest.mark.asyncio
async def test_real_helper_environment_noninteractive_and_pipes(configuration):
    script = """import json, os, sys
info = json.loads(os.environ['KUBERNETES_EXEC_INFO'])
assert info['spec']['interactive'] is False
assert sys.stdin.read() == ''
assert os.environ['TEST_EXEC'] == 'works'
sys.stderr.write('sensitive helper diagnostics' * 10000)
"""
    config, context = await auth.load_configuration(
        configuration(
            script + output(response({"token": "secret"})),
            execution={"env": [{"name": "TEST_EXEC", "value": "works"}], "interactiveMode": "IfAvailable"},
        )
    )
    assert config.api_key["BearerToken"] == "Bearer secret"
    assert context["context"]["namespace"] == "chosen"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        response({"token": "secret"}, apiVersion="wrong"),
        response({"token": "secret"}, kind="wrong"),
        response({"token": "secret", "expirationTimestamp": "2000-01-01T00:00:00Z"}),
        response({"token": "secret", "expirationTimestamp": "not-a-date"}),
        response({"clientCertificateData": "secret"}),
        response({"token": ""}),
        response({"token": "secret\nheader"}),
        response({}),
    ],
)
async def test_invalid_credentials_sanitized(configuration, document, caplog):
    with pytest.raises(ConfigException) as error:
        await auth.load_configuration(configuration(output(document)))
    assert "secret" not in str(error.value)
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_helper_failure_is_sanitized(configuration, caplog):
    with pytest.raises(ConfigException) as error:
        await auth.load_configuration(configuration("import sys\nprint('secret', file=sys.stderr)\nsys.exit(9)"))
    assert "secret" not in str(error.value) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_helper_timeout_and_cancel_reap(configuration, monkeypatch, tmp_path, cancel):
    pid_file = tmp_path / "pid"
    script = (
        "import os, time\nfrom pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)"
    )
    monkeypatch.setattr(auth, "_EXEC_TIMEOUT", 10 if cancel else 0.2)
    task = asyncio.create_task(auth.load_configuration(configuration(script)))
    if cancel:
        async with asyncio.timeout(5):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ConfigException):
        await task
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_exec_certificate_files_private_and_cleaned(configuration):
    config, _ = await auth.load_configuration(
        configuration(
            output(
                response(
                    {
                        "clientCertificateData": "certificate",
                        "clientKeyData": "private-key",
                        "expirationTimestamp": "2099-01-01T00:00:00Z",
                    }
                )
            )
        )
    )
    paths = [Path(config.cert_file), Path(config.key_file)]
    assert [path.read_text() for path in paths] == ["certificate", "private-key"]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    auth.cleanup_configuration(config)
    assert not any(path.exists() for path in paths)


@pytest.mark.asyncio
async def test_auth_provider_never_executes(configuration, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Legacy provider must never execute SDK auth")

    monkeypatch.setattr(auth._SafeExecLoader, "load_and_set", forbidden)
    with pytest.raises(auth.CliAuthenticationRequiredError):
        await auth.load_configuration(configuration(user={"auth-provider": {"name": "gcp"}}))


@pytest.mark.asyncio
async def test_interactive_required_never_executes(configuration, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Interactive helper must never execute")

    monkeypatch.setattr(auth.shell, "spawn", forbidden)
    with pytest.raises(ConfigException):
        await auth.load_configuration(configuration("", execution={"interactiveMode": "Always"}))


@pytest.mark.asyncio
async def test_static_token_and_independent_configurations(configuration):
    first, _ = await auth.load_configuration(configuration(user={"token": "one"}))
    second, _ = await auth.load_configuration(configuration(user={"token": "two"}))
    assert first is not second
    assert first.api_key["BearerToken"] == "Bearer one"
    assert second.api_key["BearerToken"] == "Bearer two"


@pytest.mark.asyncio
async def test_merged_user_relative_tls_and_token_paths(configuration, tmp_path):
    path = Path(configuration(user={"tokenFile": "token", "client-certificate": "cert", "client-key": "key"}))
    document = yaml.safe_load(path.read_text())
    users = document.pop("users")
    path.write_text(yaml.safe_dump(document))
    directory = tmp_path / "credentials"
    directory.mkdir()
    user_file = directory / "user.yaml"
    user_file.write_text(yaml.safe_dump({"users": users}))
    for name in ("token", "cert", "key"):
        (directory / name).write_text(name)
    config, _ = await auth.load_configuration(os.pathsep.join((str(path), str(user_file))))
    assert config.api_key["BearerToken"] == "Bearer token"
    assert config.cert_file == str(directory / "cert")
    assert config.key_file == str(directory / "key")


@pytest.mark.asyncio
async def test_relative_exec_and_cluster_info(configuration, tmp_path):
    script = """#!/usr/bin/env python3
import json, os
info = json.loads(os.environ['KUBERNETES_EXEC_INFO'])
assert info['spec']['cluster']['certificate-authority-data'] == 'Y2E='
assert info['spec']['cluster']['server'] == 'https://example.invalid'
assert info['spec']['cluster']['config'] == {'audience': 'test'}
"""
    helper = tmp_path / "credential-helper"
    helper.write_text(script + output(response({"token": "from-relative"})))
    helper.chmod(0o700)
    path = Path(
        configuration(
            execution={
                "apiVersion": "client.authentication.k8s.io/v1",
                "interactiveMode": "Never",
                "command": "./credential-helper",
                "provideClusterInfo": True,
            }
        )
    )
    document = yaml.safe_load(path.read_text())
    document["clusters"][0]["cluster"].update(
        {
            "certificate-authority": "ca",
            "extensions": [{"name": "client.authentication.k8s.io/exec", "extension": {"audience": "test"}}],
        }
    )
    path.write_text(yaml.safe_dump(document))
    (tmp_path / "ca").write_text("ca")
    config, _ = await auth.load_configuration(str(path))
    assert config.api_key["BearerToken"] == "Bearer from-relative"


@pytest.mark.asyncio
async def test_beta_plugin_without_interactive_mode(configuration):
    version = "client.authentication.k8s.io/v1beta1"
    path = Path(
        configuration(output(response({"token": "beta"}, apiVersion=version)), execution={"apiVersion": version})
    )
    document = yaml.safe_load(path.read_text())
    del document["users"][0]["user"]["exec"]["interactiveMode"]
    path.write_text(yaml.safe_dump(document))
    config, _ = await auth.load_configuration(str(path))
    assert config.api_key["BearerToken"] == "Bearer beta"


@pytest.mark.asyncio
async def test_invalid_config_error_does_not_expose_yaml(tmp_path):
    path = tmp_path / "config"
    path.write_text("secret-password: [invalid yaml")
    with pytest.raises(ConfigException) as error:
        await auth.load_configuration(str(path))
    assert "secret-password" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
@pytest.mark.parametrize("cancel", [False, True])
async def test_descendant_pipe_writers_are_killed(configuration, tmp_path, monkeypatch, cancel):
    ready = tmp_path / "child-ready"
    marker = tmp_path / "child-survived"
    child = (
        "import time\nfrom pathlib import Path\n"
        f'Path({str(ready)!r}).write_text("ready")\n'
        f'time.sleep(.6)\nPath({str(marker)!r}).write_text("survived")\n'
    )
    # The direct helper exits immediately: only the child retains both pipes.
    script = f'import subprocess, sys\nsubprocess.Popen([sys.executable, "-c", {child!r}])\n'
    monkeypatch.setattr(auth, "_EXEC_TIMEOUT", 5 if cancel else 0.15)
    task = asyncio.create_task(auth.load_configuration(configuration(script)))
    async with asyncio.timeout(2):
        while not ready.exists():
            await asyncio.sleep(0.005)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ConfigException):
        await asyncio.wait_for(task, 1)
    await asyncio.sleep(0.65)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_cleanup_bounds_pipe_drain(monkeypatch):
    from types import SimpleNamespace

    closed = []

    async def communicate():
        await asyncio.Event().wait()

    async def wait():
        return 0

    process = SimpleNamespace(
        pid=123456789,
        returncode=0,
        communicate=communicate,
        wait=wait,
        _transport=SimpleNamespace(close=lambda: closed.append(True)),
    )
    monkeypatch.setattr(auth, "_EXEC_CLEANUP_TIMEOUT", 0.01)
    if os.name == "posix":
        monkeypatch.setattr(auth.os, "killpg", lambda *_: None)
    await asyncio.wait_for(auth._terminate_exec(process), 0.2)
    assert closed == [True]
