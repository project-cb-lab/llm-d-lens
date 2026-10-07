"""Cluster-session registry and private kubeconfig resolution for workers."""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from hmac import compare_digest
from pathlib import Path
from uuid import uuid4

from llm_d_bench.cluster import registry
from llm_d_bench.cluster.settings import cluster_settings

_SESSION_ID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
_SESSION_DIRECTORY = cluster_settings.session_directory

_sessions: dict[str, ClusterSession] = {}


@dataclass(frozen=True)
class ClusterSession:
    id: str
    server_id: str
    kubeconfig_path: Path
    created_at: str


def _session_value(session_id: object) -> str:
    session_value = str(session_id or "")
    if not _SESSION_ID.fullmatch(session_value):
        raise ValueError("cluster_session_id is invalid")
    return session_value


def session_kubeconfig_path(session_id: str) -> Path:
    return (_SESSION_DIRECTORY / f"{session_id}.yaml").resolve()


def register_session(server_id: str, kubeconfig_path: Path, session_id: str | None = None) -> ClusterSession:
    """Record a connected session and make its kubeconfig resolvable to workers."""
    value = session_id or str(uuid4())
    session = ClusterSession(value, server_id, kubeconfig_path.resolve(), datetime.now(UTC).isoformat())
    _sessions[value] = session
    return session


def _server_id_for_kubeconfig(kubeconfig: Path) -> str | None:
    """Recover the durable cluster ID without exposing kubeconfig contents.

    Kubeconfig text now lives in the ``clusters`` table (see
    ``llm_d_bench.cluster.registry``), not one file per cluster on disk, so
    the comparison is against each cluster's DB-stored kubeconfig rather
    than a directory of ``*.kubeconfig`` files.
    """
    try:
        candidate = kubeconfig.read_bytes()
    except OSError:
        return None
    for cluster in registry.list_clusters(include_drafts=True):
        stored = registry.read_kubeconfig(cluster.id)
        if stored is not None and compare_digest(candidate, stored.encode()):
            return cluster.id
    return None


def close_session(session_id: str) -> bool:
    session = _sessions.pop(session_id, None)
    if session is None:
        return False
    session.kubeconfig_path.unlink(missing_ok=True)
    return True


def get_session_for_server(server_id: str) -> ClusterSession | None:
    for session in _sessions.values():
        if session.server_id == server_id:
            return session
    durable_sessions = (
        sorted(
            _SESSION_DIRECTORY.glob("*.yaml"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if _SESSION_DIRECTORY.is_dir()
        else []
    )
    for kubeconfig in durable_sessions:
        if not _SESSION_ID.fullmatch(kubeconfig.stem):
            continue
        if _server_id_for_kubeconfig(kubeconfig) == server_id:
            return register_session(server_id, kubeconfig, kubeconfig.stem)
    return None


def close_session_for_server(server_id: str) -> None:
    for session_id, session in list(_sessions.items()):
        if session.server_id == server_id:
            close_session(session_id)


def get_session(session_id: object) -> ClusterSession | None:
    try:
        value = _session_value(session_id)
    except ValueError:
        return None
    session = _sessions.get(value)
    if session is None:
        try:
            return require_active_session(value)
        except ValueError:
            return None
    return session


def require_active_session(session_id: object) -> ClusterSession:
    """Resolve a live cluster session without exposing its kubeconfig contents."""
    value = _session_value(session_id)
    session = _sessions.get(value)
    kubeconfig = session_kubeconfig_path(value)
    if not kubeconfig.is_file():
        raise ValueError("cluster session is no longer active")
    if stat.S_IMODE(kubeconfig.stat().st_mode) & 0o077:
        raise ValueError("cluster session kubeconfig has unsafe permissions")
    if session is None or session.server_id == "restored":
        server_id = _server_id_for_kubeconfig(kubeconfig)
        if server_id is None:
            raise ValueError("cluster session is no longer active")
        session = register_session(server_id, kubeconfig, value)
    return session


def deployment_runtime_overrides(session_id: object) -> dict[str, str]:
    """Return the private kubeconfig environment for an active cluster session."""
    if not session_id:
        return {}
    return {"KUBECONFIG": str(require_active_session(session_id).kubeconfig_path)}
