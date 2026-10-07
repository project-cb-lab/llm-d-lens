"""Database connection settings: external DSN vs. embedded PostgreSQL.

Per docs/design/sqlalchemy-data-access-layer-design.md section 4 and
docs/design/cluster-creation-wizard-design.md ("Step 0: database setup"),
Prism supports two mutually exclusive ways to obtain a database connection:

1. External: the user points us at a database instance they already run
   (``LLM_D_BENCH_DATABASE_URL``), managed entirely outside this process.
   PostgreSQL is the only engine every table/migration has been verified
   against; MySQL, Oracle, and SQL Server are accepted (see
   ``SUPPORTED_EXTERNAL_ENGINES`` and ``system_router.py``'s engine picker)
   on an experimental basis -- SQLAlchemy will happily open a connection to
   any of them, but ``alembic upgrade head`` may hit dialect-specific gaps
   (e.g. ``sa.ARRAY`` columns) until those migrations grow non-PostgreSQL
   variants.
2. Embedded: we start (and persist the data directory of) a local
   PostgreSQL server via ``pgserver`` and connect to that instead. This is
   the default so a fresh install "just works" without any external setup,
   and is always PostgreSQL (the only engine ``pgserver`` provides).

This module never touches SQLite in production; SQLite is only used as an
explicit opt-in for the test suite (see llm_d_bench/db/engine.py).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_DB_DATA_DIRECTORY_DEFAULT = Path("~/.llm-d-lens/db/data").expanduser()

DatabaseMode = Literal["external", "embedded"]

# Dialects accepted for external connections (see docstring above): only
# "postgresql" is fully verified end-to-end (migrations + repositories);
# the rest are exposed in the Step 0 wizard as an explicit experimental
# choice. Each maps to the DBAPI driver we ask SQLAlchemy to use when the
# user's DSN doesn't already name one (e.g. a bare ``mysql://...``).
ExternalDatabaseEngine = Literal["postgresql", "mysql", "oracle", "mssql"]
SUPPORTED_EXTERNAL_ENGINES: tuple[ExternalDatabaseEngine, ...] = ("postgresql", "mysql", "oracle", "mssql")
_DEFAULT_DRIVERS: dict[str, str] = {
    "postgresql": "psycopg",  # see [project.optional-dependencies] -- deliberately not psycopg2
    "mysql": "pymysql",
    "oracle": "oracledb",
    "mssql": "pyodbc",
}


class DatabaseSettingsError(Exception):
    """Raised when the configured database connection can't be resolved/started."""


@dataclass(frozen=True)
class DatabaseSettings:
    mode: DatabaseMode
    # Set when mode == "external": a full SQLAlchemy DSN
    # (e.g. postgresql+psycopg://user:pass@host:5432/dbname) supplied by the
    # user, pointing at a PostgreSQL instance they deploy/operate themselves.
    external_url: str | None
    # Set when mode == "embedded": where pgserver keeps its on-disk cluster
    # (data files), so it survives process restarts.
    embedded_data_directory: Path
    # Set when mode == "embedded" and a password was configured at install
    # time (see bootstrap_config.py). None means "stay on pgserver's default
    # trust auth, no password" -- the scripts/dev.sh local-dev case.
    embedded_password: str | None = None

    @classmethod
    def from_environment(cls) -> DatabaseSettings:
        external_url = os.environ.get("LLM_D_BENCH_DATABASE_URL")
        mode = os.environ.get("LLM_D_BENCH_DB_MODE")
        embedded_password = os.environ.get("LLM_D_BENCH_EMBEDDED_DB_PASSWORD")
        if not external_url and not mode:
            # No explicit ops-provided configuration -- fall back to whatever
            # the "Step 0" wizard flow persisted last time it ran (see
            # bootstrap_config.py / cluster-creation-wizard-design.md §4.0).
            from llm_d_bench.db.bootstrap_config import load_bootstrap_config  # noqa: PLC0415

            bootstrap = load_bootstrap_config()
            if bootstrap is not None:
                mode = bootstrap.mode
                external_url = bootstrap.database_url
                if embedded_password is None:
                    embedded_password = bootstrap.embedded_password
        mode = mode or ("embedded" if not external_url else "external")
        if mode not in ("external", "embedded"):
            raise DatabaseSettingsError(f"invalid LLM_D_BENCH_DB_MODE: {mode!r} (expected 'external' or 'embedded')")
        if mode == "external" and not external_url:
            raise DatabaseSettingsError("LLM_D_BENCH_DB_MODE=external requires LLM_D_BENCH_DATABASE_URL to be set")
        embedded_data_directory = (
            Path(os.environ.get("LLM_D_BENCH_EMBEDDED_DB_DIR", str(_DB_DATA_DIRECTORY_DEFAULT))).expanduser().resolve()
        )
        return cls(
            mode=mode,
            external_url=external_url,
            embedded_data_directory=embedded_data_directory,
            embedded_password=embedded_password if mode == "embedded" else None,
        )


def resolve_database_url(settings: DatabaseSettings) -> str:
    """Return the SQLAlchemy DSN to connect with, starting the embedded server if needed."""
    if settings.mode == "external":
        assert settings.external_url is not None
        return _normalize_driver(settings.external_url)
    return _normalize_driver(_start_embedded_postgres(settings.embedded_data_directory, settings.embedded_password))


def _normalize_driver(url: str) -> str:
    """Force our one supported DBAPI driver per dialect when the caller's DSN doesn't name one.

    Both ``pgserver.get_uri()`` and a plain user-supplied ``postgresql://...``
    DSN default to the bare ``postgresql`` dialect, which SQLAlchemy resolves
    to ``psycopg2`` -- a dependency this project deliberately doesn't install
    (see pyproject.toml: ``psycopg[binary]>=3``, not ``psycopg2``). The same
    ambiguity exists for the experimental MySQL/Oracle/SQL Server dialects
    (see ``SUPPORTED_EXTERNAL_ENGINES``), each of which has several
    community DBAPI implementations; we only install and support one driver
    per dialect. A DSN that already names an explicit driver
    (``mysql+mysqlconnector://...``) is left untouched -- the caller clearly
    wants that one.
    """
    scheme, separator, rest = url.partition("://")
    if not separator or "+" in scheme:
        return url
    driver = _DEFAULT_DRIVERS.get(scheme)
    if driver is None:
        return url
    return f"{scheme}+{driver}://{rest}"


class _EmbeddedPasswordMismatchError(Exception):
    """Raised internally when the on-disk cluster already requires a different password."""


_embedded_server: object | None = None  # pgserver.PostgresServer instance, lazily started


def _start_embedded_postgres(data_directory: Path, password: str | None = None) -> str:
    global _embedded_server
    try:
        import pgserver  # noqa: PLC0415 -- optional dependency, see [project.optional-dependencies].embedded-db
    except ImportError as exc:
        raise DatabaseSettingsError(
            "embedded PostgreSQL mode requires the 'pgserver' package; install it with "
            "`pip install llm-d-prism-backend[embedded-db]`, or set LLM_D_BENCH_DB_MODE=external "
            "with LLM_D_BENCH_DATABASE_URL pointing at your own PostgreSQL instance."
        ) from exc
    if _embedded_server is None:
        data_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _embedded_server = pgserver.get_server(str(data_directory))
    if password:
        # Cheap and idempotent (see _harden_embedded_postgres_password's
        # docstring) -- always (re)apply rather than trying to track whether
        # it already ran, so this also self-heals if a password is added,
        # changed, or was set by a different process for the same data dir.
        try:
            _harden_embedded_postgres_password(_embedded_server, password)
        except _EmbeddedPasswordMismatchError:
            # The on-disk cluster was already hardened with a *different*
            # password than the one we're asked to enforce now. If our own
            # bootstrap tracking (bootstrap_config.py) never recorded a
            # successful embedded setup, this can only be leftover state
            # from a previous run that started the server/hardened its
            # password but crashed before finishing (e.g. during
            # migrations) -- safe to wipe and reinitialize from scratch,
            # since nothing else depends on that half-finished data
            # directory. Otherwise, it's a genuine mismatch (e.g. a
            # manually edited bootstrap file) that we must not silently
            # discard data over.
            from llm_d_bench.db.bootstrap_config import load_bootstrap_config  # noqa: PLC0415

            if load_bootstrap_config() is not None:
                raise
            _embedded_server.cleanup()
            shutil.rmtree(data_directory, ignore_errors=True)
            data_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _embedded_server = pgserver.get_server(str(data_directory))
            _harden_embedded_postgres_password(_embedded_server, password)
        return _with_password(_embedded_server.get_uri(), password)
    return _embedded_server.get_uri()


def _with_password(uri: str, password: str) -> str:
    """Embed ``password`` into a pgserver-shaped URI (``...postgres:@/...``).

    ``pgserver.PostmasterInfo.get_uri()`` always emits an empty password
    (``postgres:@``) since the library itself never enforces one -- see the
    module docstring on ``_harden_embedded_postgres_password`` below. This
    swaps that empty password for our own, without hand-rolling the rest of
    the URI (host/socket_dir handling, escaping, etc.) a second time.
    """
    from urllib.parse import quote  # noqa: PLC0415

    return uri.replace("postgres:@", f"postgres:{quote(password, safe='')}@", 1)


def _harden_embedded_postgres_password(server: object, password: str) -> None:
    """Set a real password on the embedded PostgreSQL superuser.

    ``pgserver`` always runs ``initdb --auth=trust --auth-local=trust`` (see
    its ``postgres_server.py``) and never opens a TCP listener -- only a
    Unix domain socket, reachable solely by the local OS user. That's a
    reasonable default for e.g. ``scripts/dev.sh``'s local-dev database
    (``password`` is None there, so this function is never called), but the
    installer (``LensInstaller-Ubuntu-x86_64.sh``) asks the operator to set a
    password as defense in depth. Since PostgreSQL's ``trust`` auth method
    ignores whatever password is offered, connecting with our
    password-embedded URI works as long as ``pg_hba.conf`` hasn't been
    hardened *with a different password* yet -- so this function, and the
    URI construction above, are both idempotent and safe to run on every
    embedded-mode process start. If a *different* password was hardened by
    a previous run (see ``_EmbeddedPasswordMismatchError``), the caller decides
    whether that's safe to reset (``_start_embedded_postgres``) or a real
    conflict to surface.
    """
    import psycopg  # noqa: PLC0415 -- see pyproject.toml: psycopg[binary]>=3
    from psycopg import sql  # noqa: PLC0415

    authenticated_uri = _with_password(server.get_uri(), password)
    try:
        conn_ctx = psycopg.connect(authenticated_uri, autocommit=True)
    except psycopg.OperationalError as error:
        if "password authentication failed" in str(error):
            raise _EmbeddedPasswordMismatchError from error
        raise
    with conn_ctx as conn, conn.cursor() as cur:
        # ALTER ROLE doesn't accept its password as a bind parameter (it's
        # DDL, not DML) -- sql.Literal() safely quotes/escapes it instead.
        cur.execute(sql.SQL("ALTER ROLE postgres WITH PASSWORD {}").format(sql.Literal(password)))
        if _require_password_auth(server.pgdata / "pg_hba.conf"):
            cur.execute("SELECT pg_reload_conf()")


def _require_password_auth(hba_path: Path) -> bool:
    """Rewrite pgserver's generated ``trust`` auth lines in ``pg_hba.conf`` to
    require a password (``scram-sha-256``) instead. Returns True if changed.

    Only rewrites the auth-method column of active (non-comment) rules, so
    it never touches the file's explanatory comments (which also mention
    "trust" as one of several available auth methods).
    """
    lines = hba_path.read_text(encoding="utf-8").splitlines()
    changed = False
    rewritten: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped.split()[-1] == "trust":
            rewritten.append(line[: line.rfind("trust")] + "scram-sha-256")
            changed = True
        else:
            rewritten.append(line)
    if changed:
        hba_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return changed


def shutdown_embedded_postgres() -> None:
    """Best-effort clean stop of the embedded server, e.g. on application shutdown."""
    global _embedded_server
    if _embedded_server is not None:
        _embedded_server.cleanup()
        _embedded_server = None
