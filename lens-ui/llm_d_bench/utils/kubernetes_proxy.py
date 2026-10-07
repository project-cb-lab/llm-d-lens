"""Process-level proxy bypass for a cluster's own Kubernetes API server.

The Kubernetes Python SDK's aiohttp transport enables ``trust_env`` and therefore
sends API requests through the process ``HTTP(S)_PROXY``. aiohttp delegates the
per-host bypass decision to ``urllib.request.proxy_bypass``, which -- unlike the
Go-based ``kubectl`` client -- does not understand CIDR notation in ``NO_PROXY``.
A backend whose ``NO_PROXY`` covers the cluster subnet only as ``10.112.0.0/16``
still proxies the API server and fails with ``aiohttp.ClientHttpProxyError``.
Adding the exact API server host to ``NO_PROXY``/``no_proxy`` closes that gap;
the change is additive and idempotent, so every other host keeps its existing
proxy behaviour and an explicit kubeconfig ``proxy-url`` is untouched.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml

_PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")


def server_host(server: str | None) -> str | None:
    """Return the hostname/IP of a Kubernetes API server URL, if parseable."""
    if not server:
        return None
    return urlsplit(server).hostname


def api_server_host(kubeconfig_path: str | None) -> str | None:
    """Extract the API server hostname/IP from a kubeconfig file, if readable."""
    if not kubeconfig_path:
        return None
    try:
        parsed = yaml.safe_load(Path(kubeconfig_path).read_text())
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(parsed, dict):
        return None
    clusters = parsed.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        return None
    cluster_entry = clusters[0].get("cluster") if isinstance(clusters[0], dict) else None
    server = cluster_entry.get("server") if isinstance(cluster_entry, dict) else None
    return server_host(server)


def ensure_api_server_proxy_bypass(host: str | None) -> bool:
    """Add ``host`` to the process ``NO_PROXY``/``no_proxy``.

    Returns ``True`` when the environment changed. It is a no-op when no host is
    known, no proxy is configured, or the host is already listed. The Kubernetes
    aiohttp transport reads these variables at request time, so this runs in the
    backend process before the API call (see ``kubernetes_auth.load_configuration``).
    """
    if not host:
        return False
    if not any(os.environ.get(name) for name in _PROXY_NAMES):
        return False
    changed = False
    for name in _NO_PROXY_NAMES:
        entries = [entry.strip() for entry in os.environ.get(name, "").split(",") if entry.strip()]
        if host in entries:
            continue
        entries.append(host)
        os.environ[name] = ",".join(entries)
        changed = True
    return changed
