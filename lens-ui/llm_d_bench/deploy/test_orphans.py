"""Tests for orphan deployment namespace scan/cleanup."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from llm_d_bench.deploy import orphans


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Runner:
    def __init__(self, result: _Result) -> None:
        self._result = result
        self.calls: list = []

    async def run(self, argv, *, cwd=None, env=None, input=None, timeout=10):  # noqa: A002
        self.calls.append(argv)
        return self._result


class _Store:
    def __init__(self, namespaces: list[str]) -> None:
        self._namespaces = namespaces

    def list_executions(self):
        return [SimpleNamespace(namespace=name) for name in self._namespaces]


def _namespaces_json(*names: str) -> str:
    items = [
        {
            "metadata": {"name": name, "creationTimestamp": f"2026-09-0{index + 1}T00:00:00Z"},
            "status": {"phase": "Active"},
        }
        for index, name in enumerate(names)
    ]
    return json.dumps({"items": items})


@pytest.mark.asyncio
async def test_scan_returns_only_untracked_prefixed_namespaces(monkeypatch):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    runner = _Runner(
        _Result(
            stdout=_namespaces_json(
                "llmd-",
                "llmd-known",
                "llmd-orphan",
                "other-ns",
            )
        )
    )
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: runner)

    result = await orphans.scan_orphan_namespaces("cluster-a", store=_Store(["llmd-known"]))

    assert [orphan.name for orphan in result] == ["llmd-orphan"]
    assert result[0].phase == "Active"


@pytest.mark.asyncio
async def test_scan_raises_on_kubectl_failure(monkeypatch):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: _Runner(_Result(returncode=1, stderr="boom")))
    with pytest.raises(orphans.OrphanCleanupError, match="boom"):
        await orphans.scan_orphan_namespaces("cluster-a", store=_Store([]))


@pytest.mark.asyncio
async def test_clean_refuses_known_and_outside_prefix(monkeypatch):
    monkeypatch.setenv("LLM_D_BENCH_NAMESPACE_PREFIX", "llmd-")
    runner = _Runner(_Result(returncode=0))
    monkeypatch.setattr(orphans, "scoped_runner", lambda _cid: runner)

    cleaned, failed = await orphans.clean_orphan_namespaces(
        "cluster-a", ["llmd-orphan", "llmd-known", "other-ns"], store=_Store(["llmd-known"])
    )

    assert cleaned == ["llmd-orphan"]
    assert {row["name"] for row in failed} == {"llmd-known", "other-ns"}
    assert [argv[:4] for argv in runner.calls] == [["kubectl", "delete", "namespace", "llmd-orphan"]]
