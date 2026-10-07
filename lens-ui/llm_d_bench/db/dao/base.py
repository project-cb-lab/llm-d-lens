"""Common DAO base: transaction boundary + optimistic-lock conflict handling.

See docs/design/sqlalchemy-data-access-layer-design.md section 6.7. Every
concrete DAO (StorageVolumeDao, ClusterDao, ...) should
inherit from ``BaseDao`` and route every write through
``self._transaction()`` rather than opening its own ``session.begin()`` --
that is what gives all 15 tables the same "single explicit transaction per
write, READ COMMITTED isolation, dirty-write detection via ``version_id``"
guarantees for free.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from llm_d_bench.db.engine import get_session_factory


class ConcurrentUpdateError(Exception):
    """Raised when a write loses a race with another already-committed write.

    Wraps SQLAlchemy's ``StaleDataError`` (a failed ``version_id`` optimistic
    lock check) in a repository-agnostic exception that API layers can map to
    HTTP 409 Conflict. Callers should re-fetch the row and retry.
    """


class BaseDao:
    def __init__(self, session_factory: sessionmaker[Session] | None = None):
        # Resolved lazily (see _get_session_factory) rather than eagerly here:
        # several call sites cache a DAO (indirectly, via a Store
        # wrapper's own module-level singleton -- e.g.
        # llm_d_bench.storage.store.default_store()) for the lifetime of the
        # process. Capturing get_session_factory()'s return value once at
        # __init__ time would freeze that cached DAO onto whatever
        # engine happened to be active at first construction -- fatal for
        # tests, which rebind the engine before every test (see the repo
        # root conftest.py). An explicit override is still captured eagerly,
        # since a caller passing one in clearly wants that exact factory.
        self._explicit_session_factory = session_factory

    def _get_session_factory(self) -> sessionmaker[Session]:
        return self._explicit_session_factory or get_session_factory()

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        """Open a Session bound to a single explicit transaction for one write.

        Commits automatically when the ``with`` block exits normally; rolls
        back on any exception. A ``StaleDataError`` raised by SQLAlchemy's
        ``version_id_col`` mechanism (see design doc section 6.7.4) is
        translated to ``ConcurrentUpdateError`` so callers don't need to know
        about SQLAlchemy internals.
        """
        with self._get_session_factory()() as session, session.begin():
            try:
                yield session
            except StaleDataError as exc:
                raise ConcurrentUpdateError("row was concurrently modified by another transaction; retry") from exc

    @contextmanager
    def _read_only(self) -> Iterator[Session]:
        """Open a Session for read-only queries; no explicit write transaction."""
        with self._get_session_factory()() as session:
            yield session
