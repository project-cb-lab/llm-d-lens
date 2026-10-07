"""SSH remote execution and local TCP forward tunnels via paramiko."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import paramiko

_MAX_OUTPUT = 64_000


@dataclass(frozen=True)
class SshTarget:
    host: str
    port: int
    username: str
    key_filename: str | None = None
    password: str | None = None
    host_key_fingerprint: str | None = None


@dataclass(frozen=True)
class HostKeyInfo:
    algorithm: str
    fingerprint: str
    key_data: str


@dataclass(frozen=True)
class RemoteResult:
    stdout: str
    stderr: str
    returncode: int


def _drain(stream: Any) -> str:
    return stream.read().decode(errors="replace")[-_MAX_OUTPUT:]


def host_key_fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    encoded = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{encoded}"


def inspect_host_key(host: str, port: int, timeout: float = 15) -> HostKeyInfo:
    """Read a server host key without authenticating or sending credentials."""
    with socket.create_connection((host, port), timeout=timeout) as connection:
        transport = paramiko.Transport(connection)
        try:
            transport.start_client(timeout=timeout)
            key = transport.get_remote_server_key()
            return HostKeyInfo(key.get_name(), host_key_fingerprint(key), key.get_base64())
        finally:
            transport.close()


class _PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected_fingerprint: str) -> None:
        self._expected_fingerprint = expected_fingerprint

    def missing_host_key(self, client, hostname, key) -> None:
        actual_fingerprint = host_key_fingerprint(key)
        if not hmac.compare_digest(actual_fingerprint, self._expected_fingerprint):
            raise paramiko.SSHException(
                f"SSH host key fingerprint mismatch for {hostname}: expected "
                f"{self._expected_fingerprint}, received {actual_fingerprint}"
            )


def _set_host_key_policy(client: paramiko.SSHClient, target: SshTarget) -> None:
    if not target.host_key_fingerprint:
        raise ValueError("a confirmed SSH host-key fingerprint is required")
    client.set_missing_host_key_policy(_PinnedHostKeyPolicy(target.host_key_fingerprint))


def remote_exec(target: SshTarget, command: str, timeout: float | None = None) -> RemoteResult:
    """Run a command over an SSH exec channel and capture its output."""
    client = paramiko.SSHClient()
    _set_host_key_policy(client, target)
    client.connect(
        hostname=target.host,
        port=target.port,
        username=target.username,
        key_filename=target.key_filename or None,
        password=target.password,
        timeout=15,
        banner_timeout=15,
        auth_timeout=15,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        with ThreadPoolExecutor(max_workers=2) as pool:
            out_future = pool.submit(_drain, stdout)
            err_future = pool.submit(_drain, stderr)
            out = out_future.result()
            err = err_future.result()
        returncode = stdout.channel.recv_exit_status()
        return RemoteResult(stdout=out, stderr=err, returncode=returncode)
    finally:
        client.close()


def _shutdown_write(stream: Any) -> None:
    try:
        if isinstance(stream, socket.socket):
            stream.shutdown(socket.SHUT_WR)
        else:
            stream.shutdown_write()
    except OSError:
        pass


def _pump(source: Any, destination: Any) -> None:
    try:
        while True:
            data = source.recv(65_536)
            if not data:
                break
            destination.sendall(data)
    except (OSError, EOFError):
        pass
    finally:
        _shutdown_write(destination)


class SshTunnel:
    """A local TCP listener that forwards every connection through SSH to a remote host:port."""

    def __init__(self, client: paramiko.SSHClient, dest_host: str, dest_port: int) -> None:
        self._client = client
        self._transport = client.get_transport()
        self._dest = (dest_host, dest_port)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self._closed = False
        self.local_port: int = int(self._listener.getsockname()[1])

    @property
    def endpoint(self) -> str:
        return f"https://127.0.0.1:{self.local_port}"

    def start(self) -> None:
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                connection, _address = self._listener.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        try:
            channel = self._transport.open_channel(
                "direct-tcpip",
                self._dest,
                ("127.0.0.1", 0),
            )
        except Exception:
            connection.close()
            return
        upstream = threading.Thread(target=_pump, args=(connection, channel), daemon=True)
        downstream = threading.Thread(target=_pump, args=(channel, connection), daemon=True)
        upstream.start()
        downstream.start()

    def close(self) -> None:
        self._closed = True
        with contextlib.suppress(OSError):
            self._listener.close()
        with contextlib.suppress(OSError):
            self._client.close()


def create_tunnel(target: SshTarget, dest_host: str, dest_port: int) -> SshTunnel:
    """Establish an SSH session and open a local forward to the remote destination."""
    client = paramiko.SSHClient()
    _set_host_key_policy(client, target)
    client.connect(
        hostname=target.host,
        port=target.port,
        username=target.username,
        key_filename=target.key_filename or None,
        password=target.password,
        timeout=15,
        banner_timeout=15,
        auth_timeout=15,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        tunnel = SshTunnel(client, dest_host, dest_port)
    except Exception:
        client.close()
        raise
    tunnel.start()
    return tunnel
