"""Local host address discovery for cluster-reachable Lens endpoints.

The shared Gateway runs inside the cluster while the Lens control plane runs on
the host, so ext_authz needs a ``host:port`` the cluster's pods can actually
reach. These helpers enumerate candidate host/IPs to offer in (and auto-fill
into) the cluster create/edit form's per-cluster "Lens address"; the port is
Lens' own live serving port (see :func:`remember_serving_port`), never a user
setting or an env var. Pure stdlib; no cluster access.
"""

from __future__ import annotations

import os
import socket
import sys


def local_outbound_ip() -> str:
    """The host's outbound IP (used as the public host for exposed Gateways)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def local_host_addresses() -> list[str]:
    """Candidate host/IP values a cluster pod could use to reach this host.

    Ordered and de-duplicated: ``LENS_PUBLIC_HOST`` (when set), the outbound IP,
    then every other IPv4 the hostname resolves to. Loopback is excluded.
    """
    candidates: list[str] = []
    override = (os.environ.get("LENS_PUBLIC_HOST") or "").strip()
    if override:
        candidates.append(override)
    outbound = local_outbound_ip()
    if outbound:
        candidates.append(outbound)
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip and not ip.startswith("127."):
            candidates.append(ip)
    seen: set[str] = set()
    ordered: list[str] = []
    for ip in candidates:
        if ip not in seen:
            seen.add(ip)
            ordered.append(ip)
    return ordered


_SERVING_PORT: int | None = None


def serving_port_from_argv(argv: list[str] | None = None) -> int | None:
    """The ``--port`` the ASGI server was launched with (``uvicorn ... --port N``).

    Seeds the serving port before the first request so startup/background work
    (Gateway reconciles) uses the real port rather than a guess.
    """
    args = sys.argv if argv is None else argv
    for index, arg in enumerate(args):
        if arg == "--port" and index + 1 < len(args):
            candidate = args[index + 1]
        elif arg.startswith("--port="):
            candidate = arg.split("=", 1)[1]
        else:
            continue
        try:
            return int(candidate)
        except ValueError:
            return None
    return None


def remember_serving_port(port: int | None) -> None:
    """Record the port this Lens backend is actually listening on.

    Called per incoming request (from the ASGI ``server`` scope) so background
    work — Gateway installs, which have no request — can still build the exact
    ``host:port`` a cluster must use to reach this backend.
    """
    global _SERVING_PORT
    if port:
        _SERVING_PORT = port


def current_serving_port() -> int | None:
    """The last observed Lens serving port (``None`` before the first request)."""
    return _SERVING_PORT


def lens_address(host: str | None, port: int | None = None) -> str | None:
    """Combine a Lens host/IP with its serving port into a ``host:port`` address.

    ``port`` defaults to the last observed serving port (:func:`current_serving_port`).
    Returns ``None`` when there is no host (address unset / ext_authz disabled).
    """
    clean_host = (host or "").strip()
    if not clean_host:
        return None
    resolved_port = port or current_serving_port()
    if resolved_port is None:
        return clean_host
    return f"{clean_host}:{resolved_port}"
