"""Root pytest configuration.

Gives every test its own isolated, empty database: an in-memory SQLite
engine, freshly created and torn down per test via ``StaticPool`` (so the
single in-memory connection survives across the pooled checkouts a test
makes) with all tables in ``llm_d_bench.db.base.Base.metadata`` created
before the test runs. This replaces the old per-test isolation pattern of
monkeypatching ``cluster_settings`` to a fresh ``tmp_path`` (see
``llm_d_bench/cluster/registry.py``'s move from file-based storage to the
SQLAlchemy data access layer -- docs/design/sqlalchemy-data-access-layer-design.md).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool


@pytest.fixture(autouse=True)
def _isolated_test_database():
    # Imported lazily so modules that register ORM models (e.g.
    # llm_d_bench.db.models.cluster) are only loaded once pytest actually
    # starts running tests.
    # Default the whole suite to auth-disabled so existing API tests keep
    # working unauthenticated; auth-specific tests override this locally.
    from llm_d_bench.auth.settings import AuthSettings, reset_settings_override, set_settings_for_testing

    set_settings_for_testing(AuthSettings(auth_mode="disabled", allow_unauthenticated=True))

    import llm_d_bench.db.models  # noqa: F401  (registers every table on Base.metadata)
    from llm_d_bench.db import engine as db_engine
    from llm_d_bench.db.base import Base

    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        isolation_level="SERIALIZABLE",
    )

    @event.listens_for(test_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(test_engine)
    db_engine.reset_engine_for_testing(test_engine)
    try:
        yield
    finally:
        test_engine.dispose()
        reset_settings_override()
