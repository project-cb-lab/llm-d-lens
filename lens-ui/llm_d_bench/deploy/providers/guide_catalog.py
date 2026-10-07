"""Registry of server-approved deployment Guide providers."""

from __future__ import annotations

from typing import Any

from llm_d_bench.deploy.providers.guide_adapter import GuideAdapter, GuideDefinition, ValidationResult


class GuideCatalog:
    def __init__(self, adapters: list[GuideAdapter] | None = None) -> None:
        self._adapters = {adapter.discover().guide_id: adapter for adapter in adapters or []}

    def list_definitions(self) -> list[GuideDefinition]:
        return [adapter.discover() for adapter in self._adapters.values()]

    def get_adapter(self, guide_id: str) -> GuideAdapter | None:
        return self._adapters.get(guide_id)

    def validate(
        self,
        guide_id: str,
        cluster_snapshot: dict[str, Any],
        overrides: dict[str, Any],
    ) -> ValidationResult:
        adapter = self._adapters.get(guide_id)
        if adapter is None:
            return ValidationResult(accepted=False, reasons=["unsupported_guide_mapping"])
        return adapter.validate_inputs(adapter.discover(), cluster_snapshot, overrides)

    def as_response(self) -> list[dict[str, Any]]:
        return [definition.__dict__.copy() for definition in self.list_definitions()]
