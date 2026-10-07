"""Background maintenance: session TTL sweep and audit-log retention.

Design sections 5.3 and 12. Runs on a jittered interval so multiple workers do
not sweep in lockstep; the underlying deletes are idempotent, so concurrent
sweeps are safe.
"""

from __future__ import annotations

import asyncio
import logging
import random

from llm_d_bench.auth.service import AuthService, default_service

logger = logging.getLogger(__name__)


async def _maintenance_once(service: AuthService) -> None:
    try:
        deleted_sessions = service.sweep_sessions()
        if deleted_sessions:
            logger.info("auth session sweep removed %s rows", deleted_sessions)
    except Exception:  # noqa: BLE001 - maintenance must never crash the app
        logger.exception("auth session sweep failed")
    try:
        cutoff = service.audit_dao.retention_cutoff(service.settings.audit_retention_days)
        deleted_audit = service.audit_dao.delete_before(cutoff)
        if deleted_audit:
            logger.info("audit retention removed %s rows", deleted_audit)
    except Exception:  # noqa: BLE001
        logger.exception("audit retention sweep failed")


async def run_maintenance(service: AuthService | None = None, *, stop_event: asyncio.Event | None = None) -> None:
    """Loop until ``stop_event`` is set, sweeping on the configured interval."""
    service = service or default_service()
    stop_event = stop_event or asyncio.Event()
    interval = max(60, int(service.settings.session_sweep_interval_seconds))
    while not stop_event.is_set():
        await _maintenance_once(service)
        jitter = random.uniform(0, interval * 0.1)  # noqa: S311 - scheduling jitter, not security
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval + jitter)
        except TimeoutError:
            continue
