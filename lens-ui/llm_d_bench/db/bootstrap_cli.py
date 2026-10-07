"""Non-interactive entry point for "Step 0" database configuration.

This used to be a mandatory gate inside ``CreateClusterWizard.jsx`` (the
first time the wizard opened with no database configured yet, it would
block on an in-wizard setup form). Per user request, that setup has moved
to install time instead: ``scripts/LensInstaller-Ubuntu-x86_64.sh`` calls
this module (via ``python -m llm_d_bench.db.bootstrap_cli``) once, right
after the Python virtualenv is provisioned and before the backend is ever
started, so the wizard can simply assume the database is already
configured.

Reuses :func:`llm_d_bench.db.system_router.apply_database_setup` -- the
exact same logic the (still-present, for scripting/reconfiguration use)
``POST /api/v1/system/database`` endpoint runs -- so both entry points stay
in sync.
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import quote

from llm_d_bench.db.settings import DatabaseSettingsError
from llm_d_bench.db.system_router import (
    DatabaseSetupRequest,
    _ExternalConnectionError,  # noqa: PLC2701 -- shared, in-repo, internal reuse only
    _probe_external_connection,  # noqa: PLC2701 -- shared, in-repo, internal reuse only
    apply_database_setup,
    get_database_status,
)


def _build_external_database_url(
    engine: str, *, username: str, password: str, host: str, port: str, dbname: str
) -> str:
    auth = ""
    if username:
        auth = quote(username)
        if password:
            auth += f":{quote(password)}"
        auth += "@"
    port_suffix = f":{port}" if port else ""
    return f"{engine}://{auth}{host}{port_suffix}/{dbname}"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure Prism's own database (install-time Step 0).")
    parser.add_argument("--mode", choices=["embedded", "external"], default="embedded")
    parser.add_argument("--engine", default="postgresql", choices=["postgresql", "mysql", "oracle", "mssql"])
    parser.add_argument("--host", default="")
    parser.add_argument("--port", default="")
    parser.add_argument("--dbname", default="")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument(
        "--embedded-password",
        default="",
        help=(
            "password to enforce on the embedded PostgreSQL superuser (only used with --mode embedded); "
            "omit for a password-less embedded database, e.g. scripts/dev.sh's local-dev use"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="reconfigure even if a database is already configured (bootstrap file or env vars)",
    )
    parser.add_argument(
        "--test-connection",
        action="store_true",
        help=(
            "only probe connectivity to the given --mode external connection details and exit "
            "(no env vars, bootstrap file, or tables are touched) -- used by the installer to "
            "validate connection details before proceeding"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.test_connection:
        if args.mode != "external" or not args.host or not args.dbname:
            print("error: --test-connection requires --mode external, --host, and --dbname", file=sys.stderr)
            return 2
        database_url = _build_external_database_url(
            args.engine,
            username=args.username,
            password=args.password,
            host=args.host,
            port=args.port,
            dbname=args.dbname,
        )
        try:
            _probe_external_connection(database_url)
        except Exception as error:  # noqa: BLE001 -- top-level CLI boundary, surface any failure as a clean message
            print(f"error: could not connect to the database: {error}", file=sys.stderr)
            return 1
        print("connection ok")
        return 0

    if not args.force and get_database_status().configured:
        print("database already configured -- skipping (use --force to reconfigure)", file=sys.stderr)
        return 0

    if args.mode == "external":
        if not args.host or not args.dbname:
            print("error: --mode external requires --host and --dbname", file=sys.stderr)
            return 2
        database_url = _build_external_database_url(
            args.engine,
            username=args.username,
            password=args.password,
            host=args.host,
            port=args.port,
            dbname=args.dbname,
        )
    else:
        database_url = None

    request = DatabaseSetupRequest(
        mode=args.mode,
        engine=args.engine if args.mode == "external" else None,
        database_url=database_url,
        embedded_password=args.embedded_password if args.mode == "embedded" and args.embedded_password else None,
    )
    try:
        status = apply_database_setup(request)
    except _ExternalConnectionError as error:
        print(f"error: could not connect to the database: {error}", file=sys.stderr)
        return 1
    except DatabaseSettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 -- top-level CLI boundary, surface any failure as a clean message
        print(f"error: database setup failed: {error}", file=sys.stderr)
        return 1

    target = status.display_target or "(embedded)"
    print(f"database configured: mode={status.mode} engine={status.engine or ''} target={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
