"""Pluggable backend-selection strategies for a model-service group.

Only ``random`` is registered in this version (design decision D1). The
``SelectionStrategy`` protocol and registry exist so ``weighted`` / ``affinity``
can be added later without changing the routing service.

Design reference: docs/design/model-service-v2-design.md section 6.5.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Sequence
from typing import Protocol, TypeVar

T = TypeVar("T")


class SelectionStrategy(Protocol):
    name: str

    def select(self, candidates: Sequence[T], *, user_id: str, group_id: str) -> T: ...


class RandomSelection:
    """Uniformly pick one candidate. Stateless and user-agnostic."""

    name = "random"

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.SystemRandom()

    def select(self, candidates: Sequence[T], *, user_id: str, group_id: str) -> T:
        if not candidates:
            raise ValueError("no candidates to select from")
        return self._rng.choice(list(candidates))


class RoundRobinSelection:
    """Cycle through candidates in order, per group.

    The counter is process-local (this version runs a single control-plane
    instance); it advances on every successful selection for the group.
    """

    name = "round_robin"

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()

    def select(self, candidates: Sequence[T], *, user_id: str, group_id: str) -> T:
        if not candidates:
            raise ValueError("no candidates to select from")
        ordered = list(candidates)
        with self._lock:
            index = self._counters.get(group_id, 0) % len(ordered)
            self._counters[group_id] = index + 1
        return ordered[index]


class WeightedSelection:
    """Pick candidates randomly, proportionally to their integer ``weight``."""

    name = "weighted"

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.SystemRandom()

    def select(self, candidates: Sequence[T], *, user_id: str, group_id: str) -> T:
        if not candidates:
            raise ValueError("no candidates to select from")
        ordered = list(candidates)
        weights = [max(1, int(getattr(candidate, "weight", 1) or 1)) for candidate in ordered]
        return self._rng.choices(ordered, weights=weights, k=1)[0]


_STRATEGIES: dict[str, SelectionStrategy] = {
    RandomSelection.name: RandomSelection(),
    RoundRobinSelection.name: RoundRobinSelection(),
    WeightedSelection.name: WeightedSelection(),
}


def available_policies() -> list[str]:
    """Names of the selection policies that are actually implemented."""
    return sorted(_STRATEGIES)


def get_strategy(name: str) -> SelectionStrategy:
    try:
        return _STRATEGIES[name]
    except KeyError as error:
        raise ValueError(f"selection policy is not implemented: {name}") from error


def select_member(candidates: Sequence[T], policy: str, *, user_id: str, group_id: str) -> T:
    return get_strategy(policy).select(candidates, user_id=user_id, group_id=group_id)
