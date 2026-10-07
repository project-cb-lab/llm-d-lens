"""Read llm-d EPP token counters from Prometheus into per-identity totals.

Under llm-d Gateway Mode the EPP (``llm-d-router``) reports each request's token
counts as Prometheus histograms named ``llm_d_epp_request_{input,output}_tokens``
with labels ``model_name``, ``target_model_name``, ``fairness_id`` and ``priority``.
The ``x-llm-d-inference-fairness-id`` request header (injected by the Gateway from
Lens' ``/internal/model-gateway/authorize`` decision) becomes the ``fairness_id``
label, which is how Lens attributes gateway traffic back to a user / API key.

The EPP does **not** expose a cached-prompt-token counter (only the input/output
histograms above and prefix-indexer hit *bytes*/*ratio*), so Lens records no
``cached_input_tokens`` from it; the ledger field stays 0 until an engine exposes
that number.

Lens already installs a central Prometheus per cluster that scrapes the EPP
metrics endpoint (which itself is auth-gated), so this reads instant-query
results rather than scraping the EPP. Only ``_sum`` (tokens) and ``_count``
(requests) are read; the counters are cumulative, so callers diff successive
snapshots (see ``usage_sync``).

Design reference: ``docs/design/model-service-llmd-routing-design.zh-CN.md``
sections 7-9.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Snapshot counter field -> PromQL aggregation of the matching EPP histogram.
#: The EPP exposes no cached-token counter, so ``cached_input_tokens`` is not read.
TOKEN_QUERIES = {
    "input_tokens": "sum by (model_name, fairness_id) (llm_d_epp_request_input_tokens_sum)",
    "output_tokens": "sum by (model_name, fairness_id) (llm_d_epp_request_output_tokens_sum)",
    # Requests are counted once, from the input-token family.
    "requests": "sum by (model_name, fairness_id) (llm_d_epp_request_input_tokens_count)",
}
DEFAULT_FAIRNESS_ID = "default-flow"


@dataclass(frozen=True)
class EppTokenUsage:
    """Cumulative token counters the EPP has observed for one (model, fairness_id)."""

    model_name: str
    fairness_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    requests: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.model_name, self.fairness_id)


#: Counter fields a snapshot can carry; a caller may merge any of them (the
#: default EPP sync only queries the ones in ``TOKEN_QUERIES``).
COUNTER_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "requests")


def _sample_value(sample: dict) -> int:
    value = sample.get("value")
    if not value or len(value) < 2:
        return 0
    try:
        return int(float(value[1]))
    except (TypeError, ValueError):
        return 0


def parse_token_query_results(results_by_field: dict[str, list[dict]]) -> dict[tuple[str, str], EppTokenUsage]:
    """Merge Prometheus instant-query results (keyed by counter field) into usage.

    Each series is labelled by ``model_name`` (falling back to
    ``target_model_name``) and ``fairness_id``; values are summed in case the
    query returned several series for the same pair.
    """
    acc: dict[tuple[str, str], dict[str, int]] = {}
    for field, results in results_by_field.items():
        if field not in COUNTER_FIELDS:
            continue
        for sample in results or []:
            labels = sample.get("metric") or {}
            model_name = labels.get("model_name") or labels.get("target_model_name") or "unknown"
            fairness_id = labels.get("fairness_id") or DEFAULT_FAIRNESS_ID
            row = acc.setdefault(
                (model_name, fairness_id),
                {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "requests": 0},
            )
            row[field] += _sample_value(sample)
    return {key: EppTokenUsage(model_name=key[0], fairness_id=key[1], **counters) for key, counters in acc.items()}
