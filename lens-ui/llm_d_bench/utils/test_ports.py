"""Tests for the local TCP port helpers."""

from __future__ import annotations

import socket

from llm_d_bench.utils.ports import port_available, valid_port


def test_valid_port_bounds():
    assert valid_port(1) and valid_port(8443) and valid_port(65535)
    assert not valid_port(0) and not valid_port(65536) and not valid_port(-1)


def test_port_available_reflects_a_live_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    host, port = sock.getsockname()
    try:
        assert port_available(host, port) is False
    finally:
        sock.close()
    assert port_available(host, port) is True
