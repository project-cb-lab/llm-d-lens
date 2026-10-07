"""SQLAlchemy Engine/Session construction.

Per docs/design/sqlalchemy-data-access-layer-design.md section 6.7: a
single process-wide Engine + sessionmaker, explicit ``READ COMMITTED``
isolation (so nobody accidentally relies on a dialect's implicit default),
short-lived Sessions per DAO call (no long-held sessions), and a
SQLite compatibility shim (foreign keys + ``version_id`` optimistic locking
both need real per-row locking/constraint semantics that SQLite only gives
you with these pragmas) used exclusively by the test suite.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from llm_d_bench.db.settings import DatabaseSettings, resolve_database_url

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def build_engine(settings: DatabaseSettings | None = None) -> Engine:
    settings = settings or DatabaseSettings.from_environment()
    url = resolve_database_url(settings)
    engine = create_engine(
        url,
        isolation_level="READ COMMITTED" if not _is_sqlite(url) else "SERIALIZABLE",
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
    )
    if _is_sqlite(url):
        # SQLite defaults to foreign_keys=OFF and DEFERRED transactions; both
        # need to be corrected for it to be a faithful stand-in for
        # PostgreSQL in tests (see design doc section 6.7.6). Using
        # isolation_level="SERIALIZABLE" here is SQLAlchemy's documented way
        # to get pysqlite to emit "BEGIN IMMEDIATE" instead of its default
        # deferred (and slightly broken) autocommit-adjacent behavior.
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def reset_engine_for_testing(engine: Engine) -> None:
    """Point the module-level singletons at a caller-provided (test) engine."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False)


def configure_engine(settings: DatabaseSettings) -> Engine:
    """Rebuild the process-wide engine/session factory for freshly-changed settings.

    Used by the "Step 0" database setup endpoint (see
    ``llm_d_bench/db/system_router.py``) right after a user connects an
    external database or opts into the embedded one -- every DAO
    call made after this returns picks up the new connection, since
    ``BaseDao`` always resolves ``get_session_factory()`` fresh per
    call rather than caching it (see design doc section 6.7.1).
    """
    global _engine, _session_factory
    new_engine = build_engine(settings)
    old_engine = _engine
    _engine = new_engine
    _session_factory = sessionmaker(bind=new_engine, expire_on_commit=False)
    if old_engine is not None:
        old_engine.dispose()
    return new_engine
