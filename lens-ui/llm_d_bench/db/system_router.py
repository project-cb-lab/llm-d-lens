"""``Step 0`` of the cluster creation wizard: application-level database setup.

See docs/design/cluster-creation-wizard-design.md section 4.0 and
docs/design/sqlalchemy-data-access-layer-design.md section 4 for the full
design. This module intentionally has nothing to do with any particular
cluster -- it configures how the *Prism backend process itself* connects to
its own database, a one-time, global (not per-cluster) step that gates the
rest of the wizard only the first time the backend is ever run without a
database already configured (via env vars or a previous Step 0 submission).
"""

from __future__ import annotations

import os
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import create_engine, text

from llm_d_bench.db import engine as db_engine
from llm_d_bench.db.bootstrap_config import (
    DatabaseBootstrapConfig,
    StrictModel,
    load_bootstrap_config,
    save_bootstrap_config,
)
from llm_d_bench.db.settings import (
    SUPPORTED_EXTERNAL_ENGINES,
    DatabaseSettings,
    DatabaseSettingsError,
    ExternalDatabaseEngine,
    resolve_database_url,
)

router = APIRouter(prefix="/api/v1/system", tags=["system"])

# pip extra (see [project.optional-dependencies] in pyproject.toml) that
# installs the DBAPI driver for each experimental external engine, surfaced
# in error messages when a connection attempt fails for lack of it.
_DRIVER_EXTRA_HINT: dict[str, str] = {"mysql": "mysql", "oracle": "oracle", "mssql": "mssql"}


class DatabaseStatus(StrictModel):
    """Response for ``GET /api/v1/system/database``.

    Never echoes back ``database_url`` verbatim (it may contain a password)
    -- only a masked host/port/dbname summary for display.
    """

    configured: bool
    mode: Literal["external", "embedded"] | None = None
    engine: ExternalDatabaseEngine | None = None
    display_target: str | None = Field(default=None, alias="displayTarget")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DatabaseSetupRequest(StrictModel):
    mode: Literal["external", "embedded"]
    # Only meaningful (and required) when mode == "external"; embedded mode
    # is always PostgreSQL (the only engine `pgserver` provides), see
    # settings.py. Defaults to "postgresql" for external mode too, so
    # existing plain-DSN callers keep working unchanged.
    engine: ExternalDatabaseEngine | None = None
    database_url: str | None = Field(default=None, alias="databaseUrl")
    # Only meaningful when mode == "embedded": a password to enforce on the
    # embedded PostgreSQL superuser (see settings.py's
    # _harden_embedded_postgres_password). Optional -- omit for a
    # password-less embedded database (e.g. scripts/dev.sh's local-dev use).
    embedded_password: str | None = Field(default=None, alias="embeddedPassword")

    @model_validator(mode="after")
    def _validate(self) -> DatabaseSetupRequest:
        if self.mode == "external":
            url = (self.database_url or "").strip()
            if not url:
                raise ValueError("external mode requires databaseUrl")
            engine = self.engine or "postgresql"
            scheme_dialect = urlsplit(url).scheme.split("+", 1)[0]
            if scheme_dialect != engine:
                raise ValueError(f"databaseUrl scheme {scheme_dialect!r} does not match the selected engine {engine!r}")
            self.engine = engine
            if self.embedded_password:
                raise ValueError("external mode must not include embeddedPassword")
        else:
            if self.database_url:
                raise ValueError("embedded mode must not include databaseUrl")
            if self.engine not in (None, "postgresql"):
                raise ValueError("embedded mode is always PostgreSQL; engine must be omitted or 'postgresql'")
            self.engine = "postgresql"
        return self


def _display_target(url: str) -> str:
    parsed = urlsplit(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path.lstrip("/")
    return f"{netloc}/{path}" if path else netloc


def _dialect_of(url: str) -> ExternalDatabaseEngine | None:
    dialect = urlsplit(url).scheme.split("+", 1)[0]
    return dialect if dialect in SUPPORTED_EXTERNAL_ENGINES else None


def get_database_status() -> DatabaseStatus:
    env_url = os.environ.get("LLM_D_BENCH_DATABASE_URL")
    env_mode = os.environ.get("LLM_D_BENCH_DB_MODE")
    if env_url or env_mode:
        # An ops-provided environment configuration always wins and is
        # always considered "already configured" -- Step 0 must never
        # override it (see design doc §4.0).
        settings = DatabaseSettings.from_environment()
        if settings.mode == "external":
            assert settings.external_url is not None
            return DatabaseStatus(
                configured=True,
                mode="external",
                engine=_dialect_of(settings.external_url),
                display_target=_display_target(settings.external_url),
            )
        return DatabaseStatus(configured=True, mode="embedded", engine="postgresql")
    bootstrap = load_bootstrap_config()
    if bootstrap is None:
        return DatabaseStatus(configured=False)
    if bootstrap.mode == "external":
        assert bootstrap.database_url is not None
        return DatabaseStatus(
            configured=True,
            mode="external",
            engine=_dialect_of(bootstrap.database_url),
            display_target=_display_target(bootstrap.database_url),
        )
    return DatabaseStatus(configured=True, mode="embedded", engine="postgresql")


def _probe_external_connection(database_url: str) -> None:
    from pathlib import Path  # noqa: PLC0415

    probe_settings = DatabaseSettings(mode="external", external_url=database_url, embedded_data_directory=Path("."))
    resolved_url = resolve_database_url(probe_settings)
    try:
        probe_engine = create_engine(resolved_url)
    except Exception as error:
        raise RuntimeError(_with_driver_hint(database_url, error)) from error
    try:
        with probe_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:
        raise RuntimeError(_with_driver_hint(database_url, error)) from error
    finally:
        probe_engine.dispose()


def _with_driver_hint(database_url: str, error: Exception) -> str:
    dialect = urlsplit(database_url).scheme.split("+", 1)[0]
    extra = _DRIVER_EXTRA_HINT.get(dialect)
    if extra is None:
        return str(error)
    return f"{error} (if this is a missing driver, install it with `pip install llm-d-prism-backend[{extra}]`)"


def _create_tables() -> None:
    """Create every table in ``Base.metadata`` if it doesn't already exist.

    This is the very first time this backend has ever had a database, so
    there's no existing schema to preserve/diff against -- ``create_all()``
    (create-only, never alters an existing table) is sufficient and avoids
    needing a migration history for a database that doesn't exist yet.
    Alembic (``alembic.ini`` / ``llm_d_bench/db/migrations``) stays in place
    for *future* schema changes: once a change to a Row class is needed,
    generate a migration with ``alembic revision --autogenerate`` against
    this freshly created schema and apply it with ``alembic upgrade head``.
    """
    # Imported lazily, and only here, so every ORM model module is loaded
    # (registering its table on ``Base.metadata``) before ``create_all`` runs.
    import llm_d_bench.db.models  # noqa: F401  (registers every table on Base.metadata)
    from llm_d_bench.db.base import Base

    Base.metadata.create_all(db_engine.get_engine())


class _ExternalConnectionError(RuntimeError):
    """Raised by :func:`apply_database_setup` when an external connection probe fails."""


def apply_database_setup(request: DatabaseSetupRequest) -> DatabaseStatus:
    """Shared implementation behind ``POST /api/v1/system/database``.

    Also used directly (without going through HTTP/FastAPI) by
    ``llm_d_bench.db.bootstrap_cli``, the non-interactive entry point the
    installer (``scripts/LensInstaller-Ubuntu-x86_64.sh``) uses to configure
    the database before the backend is ever started -- see that module's
    docstring. Raises plain exceptions (``_ExternalConnectionError``,
    ``DatabaseSettingsError``, or any other) rather than ``HTTPException`` so
    both callers can translate failures however fits their context.
    """
    if request.mode == "external":
        assert request.database_url is not None
        try:
            _probe_external_connection(request.database_url)
        except Exception as error:
            raise _ExternalConnectionError(str(error)) from error

    os.environ["LLM_D_BENCH_DB_MODE"] = request.mode
    if request.mode == "external":
        os.environ["LLM_D_BENCH_DATABASE_URL"] = request.database_url or ""
        os.environ.pop("LLM_D_BENCH_EMBEDDED_DB_PASSWORD", None)
    else:
        os.environ.pop("LLM_D_BENCH_DATABASE_URL", None)
        if request.embedded_password:
            os.environ["LLM_D_BENCH_EMBEDDED_DB_PASSWORD"] = request.embedded_password
        else:
            os.environ.pop("LLM_D_BENCH_EMBEDDED_DB_PASSWORD", None)

    settings = DatabaseSettings.from_environment()
    db_engine.configure_engine(settings)
    _create_tables()

    save_bootstrap_config(
        DatabaseBootstrapConfig(
            mode=request.mode,
            database_url=request.database_url,
            embedded_password=request.embedded_password,
        )
    )
    return get_database_status()


@router.get("/database", response_model=DatabaseStatus)
async def read_database_status() -> DatabaseStatus:
    return get_database_status()


@router.post("/database", response_model=DatabaseStatus)
async def setup_database(request: DatabaseSetupRequest) -> DatabaseStatus:
    try:
        return apply_database_setup(request)
    except _ExternalConnectionError as error:
        raise HTTPException(status_code=422, detail=f"Could not connect to the database: {error}") from error
    except DatabaseSettingsError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=409, detail=f"Database setup failed: {error}") from error
