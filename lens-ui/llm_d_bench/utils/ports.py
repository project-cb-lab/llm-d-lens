"""Local TCP port helpers.

Used by the model-service data plane to pick/validate the Edge Envoy listen port
and to surface a clear conflict instead of a failed process start.
"""

from __future__ import annotations

import socket

MIN_PORT = 1
MAX_PORT = 65535


def valid_port(port: int) -> bool:
    return MIN_PORT <= int(port) <= MAX_PORT


def port_available(host: str, port: int) -> bool:
    """Return True when ``host:port`` can be bound right now.

    A free port is confirmed with a real bind (SO_REUSEADDR), so TIME_WAIT
    sockets do not masquerade as available. ``host`` may be ``0.0.0.0`` to test
    all interfaces, matching how Edge Envoy binds.
    """
    if not valid_port(port):
        return False
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()
