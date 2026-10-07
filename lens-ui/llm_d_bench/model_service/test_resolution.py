"""Resolve a published Model Service group to a currently healthy member.

Both Evaluation and Simulation route "use an existing endpoint" traffic
through this shared resolver instead of a raw deployment execution, since
only a Model Service group is continuously health-probed and authorization-
gated (see llm_d_bench/model_service/resolution.py for the rationale).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from llm_d_bench.model_service.resolution import resolve_model_service_target


class _FakeGroups:
    def __init__(self, group):
        self._group = group

    def get(self, group_id):
        return self._group if group_id == self._group.id else None


class _FakeModelService:
    def __init__(self, group, members):
        self.groups = _FakeGroups(group)
        self._members = members

    def authorized_members(self, group, principal):  # noqa: ARG002
        return self._members


def _group(status="active"):
    return SimpleNamespace(id="msg-1", name="qwen3-prod", status=status, selection_policy="random")


def _member(execution_id="exec-1"):
    return SimpleNamespace(execution_id=execution_id)


def test_resolve_model_service_target_picks_an_authorized_member(monkeypatch):
    group = _group()
    service = _FakeModelService(group, [_member("exec-1")])
    monkeypatch.setattr("llm_d_bench.model_service.service.default_service", lambda: service)

    execution_id, published_name = resolve_model_service_target("msg-1", http_request=None)

    assert execution_id == "exec-1"
    assert published_name == "qwen3-prod"


def test_resolve_model_service_target_rejects_unknown_group(monkeypatch):
    service = _FakeModelService(_group(), [])
    monkeypatch.setattr("llm_d_bench.model_service.service.default_service", lambda: service)

    with pytest.raises(HTTPException) as excinfo:
        resolve_model_service_target("does-not-exist", http_request=None)
    assert excinfo.value.status_code == 404


def test_resolve_model_service_target_rejects_inactive_group(monkeypatch):
    service = _FakeModelService(_group(status="disabled"), [_member()])
    monkeypatch.setattr("llm_d_bench.model_service.service.default_service", lambda: service)

    with pytest.raises(HTTPException) as excinfo:
        resolve_model_service_target("msg-1", http_request=None)
    assert excinfo.value.status_code == 404


def test_resolve_model_service_target_rejects_when_no_member_is_healthy(monkeypatch):
    service = _FakeModelService(_group(), [])
    monkeypatch.setattr("llm_d_bench.model_service.service.default_service", lambda: service)

    with pytest.raises(HTTPException) as excinfo:
        resolve_model_service_target("msg-1", http_request=None)
    assert excinfo.value.status_code == 409
