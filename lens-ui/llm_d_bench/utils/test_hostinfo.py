"""Tests for local host address discovery."""

from llm_d_bench.utils import hostinfo


def test_local_host_addresses_orders_dedupes_and_skips_loopback(monkeypatch):
    monkeypatch.setenv("LENS_PUBLIC_HOST", "lens.example.com")
    monkeypatch.setattr(hostinfo, "local_outbound_ip", lambda: "10.0.0.5")
    monkeypatch.setattr(
        hostinfo.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (None, None, None, None, ("127.0.0.1", 0)),
            (None, None, None, None, ("10.0.0.5", 0)),
            (None, None, None, None, ("192.168.1.7", 0)),
        ],
    )
    assert hostinfo.local_host_addresses() == ["lens.example.com", "10.0.0.5", "192.168.1.7"]


def test_local_host_addresses_without_override(monkeypatch):
    monkeypatch.delenv("LENS_PUBLIC_HOST", raising=False)
    monkeypatch.setattr(hostinfo, "local_outbound_ip", lambda: "")
    monkeypatch.setattr(hostinfo.socket, "getaddrinfo", lambda *a, **k: [])
    assert hostinfo.local_host_addresses() == []


def test_lens_address_uses_remembered_serving_port(monkeypatch):
    monkeypatch.setattr(hostinfo, "_SERVING_PORT", None)
    assert hostinfo.current_serving_port() is None
    assert hostinfo.lens_address("10.0.0.5") == "10.0.0.5"

    hostinfo.remember_serving_port(8084)
    assert hostinfo.current_serving_port() == 8084
    assert hostinfo.lens_address("10.0.0.5") == "10.0.0.5:8084"
    assert hostinfo.lens_address("10.0.0.5", 9999) == "10.0.0.5:9999"


def test_lens_address_none_without_host():
    assert hostinfo.lens_address(None) is None
    assert hostinfo.lens_address("  ") is None


def test_serving_port_from_argv():
    assert hostinfo.serving_port_from_argv(["uvicorn", "app", "--host", "0.0.0.0", "--port", "8084"]) == 8084
    assert hostinfo.serving_port_from_argv(["uvicorn", "app", "--port=9100"]) == 9100
    assert hostinfo.serving_port_from_argv(["uvicorn", "app"]) is None
    assert hostinfo.serving_port_from_argv(["uvicorn", "app", "--port", "nope"]) is None
