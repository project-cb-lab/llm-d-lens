"""Tests for the kubectl discovery-cache helpers in ``llm_d_bench.utils.shell``."""

from __future__ import annotations

import asyncio
from pathlib import Path

from llm_d_bench.utils.shell import (
    ShellClient,
    _with_kubectl_cache_dir,
    invalidate_kubectl_discovery_cache,
    kubectl_cache_dir,
)


def test_kubectl_cache_dir_is_keyed_by_kubeconfig(tmp_path):
    dir_a = kubectl_cache_dir(str(tmp_path / "cluster-a.kubeconfig"))
    dir_b = kubectl_cache_dir(str(tmp_path / "cluster-b.kubeconfig"))
    assert dir_a != dir_b
    # Deterministic: same kubeconfig always resolves to the same cache dir.
    assert dir_a == kubectl_cache_dir(str(tmp_path / "cluster-a.kubeconfig"))


def test_with_kubectl_cache_dir_matches_kubectl_cache_dir_helper(tmp_path):
    kubeconfig = str(tmp_path / "cluster-a.kubeconfig")
    argv = _with_kubectl_cache_dir(["kubectl", "get", "pods"], {"KUBECONFIG": kubeconfig})
    expected = f"--cache-dir={kubectl_cache_dir(kubeconfig)}"
    assert argv[1] == expected


def test_spawn_with_executable_keeps_arguments_literal(tmp_path):
    script = tmp_path / "record-argument.sh"
    marker = tmp_path / "injected"
    output = tmp_path / "captured.txt"
    payload = f"; touch {marker}; #"
    script.write_text('#!/bin/sh\nprintf "%s" "$1" > "$2"\n', encoding="utf-8")
    script.chmod(0o755)

    async def run() -> int:
        process = await ShellClient().spawn_with_executable(script, [payload, str(output)])
        return await process.wait()

    assert asyncio.run(run()) == 0
    assert output.read_text(encoding="utf-8") == payload
    assert not marker.exists()


def test_invalidate_kubectl_discovery_cache_removes_discovery_and_http_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kubeconfig = str(tmp_path / "cluster-a.kubeconfig")
    cache_dir = kubectl_cache_dir(kubeconfig)
    discovery_dir = cache_dir / "discovery" / "some-server"
    http_dir = cache_dir / "http"
    other_dir = cache_dir / "not-a-cache-subdir"
    discovery_dir.mkdir(parents=True)
    http_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    (discovery_dir / "servergroups.json").write_text("{}")
    (http_dir / "entry").write_text("cached")
    (other_dir / "keep-me").write_text("unrelated")

    invalidate_kubectl_discovery_cache(kubeconfig)

    assert not (cache_dir / "discovery").exists()
    assert not (cache_dir / "http").exists()
    assert other_dir.exists()  # unrelated cache-dir contents are left alone


def test_invalidate_kubectl_discovery_cache_is_a_noop_when_nothing_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Should not raise even though the cache dir was never created.
    invalidate_kubectl_discovery_cache(str(tmp_path / "never-used.kubeconfig"))
