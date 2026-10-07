"""Request-scoped SDK configuration and noninteractive exec credentials."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from kubernetes.aio.client import Configuration
from kubernetes.aio.config.config_exception import ConfigException
from kubernetes.aio.config.kube_config import KubeConfigLoader, KubeConfigMerger

from llm_d_bench.utils.kubernetes_proxy import ensure_api_server_proxy_bypass, server_host
from llm_d_bench.utils.shell import shell

_EXEC_TIMEOUT = 15.0
_EXEC_CLEANUP_TIMEOUT = 2.0
_EXEC_VERSIONS = {"client.authentication.k8s.io/v1", "client.authentication.k8s.io/v1beta1"}


class CliAuthenticationRequiredError(Exception):
    """Selected legacy auth-provider needs CLI authentication, before any I/O."""


async def _terminate_exec(process):
    # Descendants can retain the pipes even after the direct helper has exited.
    # Each POSIX helper owns a new process group so cleanup reaches those writers.
    with suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    try:
        await asyncio.wait_for(process.communicate(), _EXEC_CLEANUP_TIMEOUT)
    except TimeoutError:
        # On platforms without process groups (or a helper that deliberately
        # detaches), inherited pipes must not hold the request open indefinitely.
        process._transport.close()
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), _EXEC_CLEANUP_TIMEOUT)


async def _run_exec(execution: dict, base_path: str, cluster: dict) -> dict:
    version = execution.get("apiVersion")
    if version not in _EXEC_VERSIONS:
        raise ValueError("Unsupported exec credential version")
    mode = execution.get("interactiveMode", "IfAvailable" if version.endswith("v1beta1") else None)
    if mode not in {"Never", "IfAvailable"}:
        raise ValueError("Exec credentials require unavailable interactive input")
    command = execution.get("command")
    arguments = execution.get("args", [])
    if not isinstance(command, str) or not command or not isinstance(arguments, list):
        raise ValueError("Invalid exec command")
    if any(not isinstance(argument, str) for argument in arguments):
        raise ValueError("Invalid exec arguments")
    if not os.path.isabs(command) and ("/" in command or os.sep in command):
        command = os.path.join(base_path, command)
    environment = os.environ.copy()
    for entry in execution.get("env", []):
        name, value = entry["name"], entry["value"]
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("Invalid exec environment")
        environment[name] = value
    specification = {"interactive": False}
    if execution.get("provideClusterInfo"):
        cluster_info = {
            key: value
            for key, value in cluster.items()
            if key
            in {
                "server",
                "tls-server-name",
                "insecure-skip-tls-verify",
                "certificate-authority-data",
                "proxy-url",
                "disable-compression",
            }
        }
        if cluster.get("certificate-authority") and not cluster_info.get("certificate-authority-data"):
            cluster_info["certificate-authority-data"] = base64.b64encode(
                Path(cluster["certificate-authority"]).read_bytes()
            ).decode()
        for extension in cluster.get("extensions", []):
            if extension.get("name") == "client.authentication.k8s.io/exec":
                cluster_info["config"] = extension.get("extension")
        specification["cluster"] = cluster_info
    environment["KUBERNETES_EXEC_INFO"] = json.dumps(
        {"apiVersion": version, "kind": "ExecCredential", "spec": specification}
    )
    process = await shell.spawn(
        [command, *arguments],
        env=environment,
        cwd=base_path or None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), _EXEC_TIMEOUT)
    except (TimeoutError, asyncio.CancelledError):
        await _terminate_exec(process)
        raise
    if process.returncode:
        raise ValueError("Exec helper failed")
    result = json.loads(stdout)
    if result.get("apiVersion") != version or result.get("kind") != "ExecCredential":
        raise ValueError("Invalid exec credential response")
    status = result["status"]
    if not isinstance(status, dict):
        raise ValueError("Invalid exec credential status")
    if "expirationTimestamp" in status:
        expiration = datetime.fromisoformat(status["expirationTimestamp"].replace("Z", "+00:00"))
        if expiration.tzinfo is None or expiration <= datetime.now(UTC):
            raise ValueError("Expired exec credentials")
    return status


class _SafeExecLoader(KubeConfigLoader):
    async def load_from_exec_plugin(self):
        try:
            status = await _run_exec(
                self._user["exec"].value, self._get_base_path(self._user.path), self._cluster.value
            )
            if "token" in status:
                token = status["token"]
                if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
                    raise ValueError("Invalid exec token")
                self.token = "Bearer " + token
            else:
                certificate, key = status["clientCertificateData"], status["clientKeyData"]
                if not isinstance(certificate, str) or not certificate or not isinstance(key, str) or not key:
                    raise ValueError("Invalid exec certificate")
                self._exec_files = tempfile.TemporaryDirectory(prefix="kubernetes-exec-")
                for attribute, content in (("cert_file", certificate), ("key_file", key)):
                    path = Path(self._exec_files.name) / attribute
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "w") as stream:
                        stream.write(content)
                    setattr(self, attribute, str(path))
            return True
        except Exception:
            raise ConfigException("Kubernetes exec authentication failed") from None


def cleanup_configuration(configuration: Configuration) -> None:
    """Remove exec certificate/key files after the owning ApiClient is closed."""
    files = getattr(configuration, "_exec_credential_files", None)
    if files is not None:
        files.cleanup()
        configuration._exec_credential_files = None


async def load_configuration(config_file: str) -> tuple[Configuration, dict]:
    """Load independent selected credentials, without persisting or global defaults.

    Credentials are refreshed per operation; they are not cached across requests.
    Call cleanup_configuration after closing the client using this configuration.
    Static embedded TLS files retain the SDK's process-lifetime cleanup behavior.
    """
    loader = None
    try:
        merged = KubeConfigMerger(config_file)
        loader = _SafeExecLoader(config_dict=merged.config, config_base_path=None)
        if loader._user and "auth-provider" in loader._user:
            raise CliAuthenticationRequiredError
        # SDK 36 resolves client TLS paths against the cluster's file; merged
        # configurations require the selected user's source path instead.
        for node, fields in (
            (loader._user, ("client-certificate", "client-key")),
            (loader._cluster, ("certificate-authority",)),
        ):
            if node:
                for field in fields:
                    value = node.safe_get(field)
                    if value and not os.path.isabs(value):
                        node.value[field] = os.path.join(loader._get_base_path(node.path), value)
        configuration = Configuration()
        await loader.load_and_set(configuration)
        if not configuration.proxy:
            # The aiohttp transport ignores CIDR NO_PROXY entries, so name the
            # selected API server explicitly before any SDK request is sent.
            ensure_api_server_proxy_bypass(server_host(configuration.host))
        if hasattr(loader, "_exec_files"):
            configuration._exec_credential_files = loader._exec_files
        return configuration, loader.current_context
    except BaseException as error:
        if loader is not None and hasattr(loader, "_exec_files"):
            loader._exec_files.cleanup()
        if isinstance(error, (asyncio.CancelledError, CliAuthenticationRequiredError, KeyboardInterrupt, SystemExit)):
            raise
        raise ConfigException("Kubernetes configuration or authentication failed") from None
