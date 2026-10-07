"""Application-level database configuration ("Step 0" of the cluster creation
wizard -- see docs/design/cluster-creation-wizard-design.md section 4.0).

This describes *how the Prism backend process itself connects to its own
database*, so it cannot be stored in that database (chicken and egg). It is
persisted in a small standalone file, using the same atomic
temp-file-plus-``os.replace`` write pattern as every other file-backed Store
in this codebase, at a path that exists before any database connection is
ever made: ``~/.llm-d-lens/db-bootstrap.json`` (overridable via
``LLM_D_BENCH_DB_BOOTSTRAP_FILE``, e.g. to point at a mounted persistent
volume in a container deployment).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DatabaseBootstrapConfig(StrictModel):
    mode: Literal["external", "embedded"]
    # Required when mode == "external"; never masked on disk (this file is
    # the single source of truth for "how to connect"), but never echoed
    # back verbatim by any read API -- see DatabaseStatus.
    database_url: str | None = Field(default=None, alias="databaseUrl")
    # Optional when mode == "embedded": a password to enforce on the embedded
    # PostgreSQL superuser instead of pgserver's default trust-based (no
    # password) auth -- see settings.py's _harden_embedded_postgres_password.
    # Left unset (None) for e.g. scripts/dev.sh's local-dev embedded database,
    # which intentionally stays password-less for convenience.
    embedded_password: str | None = Field(default=None, alias="embeddedPassword")
    configured_at: datetime = Field(default_factory=utcnow, alias="configuredAt")


def _bootstrap_config_path() -> Path:
    default = Path.home() / ".llm-d-lens" / "db-bootstrap.json"
    return Path(os.environ.get("LLM_D_BENCH_DB_BOOTSTRAP_FILE", str(default))).expanduser()


def load_bootstrap_config() -> DatabaseBootstrapConfig | None:
    path = _bootstrap_config_path()
    if not path.is_file():
        return None
    return DatabaseBootstrapConfig.model_validate_json(path.read_text(encoding="utf-8"))


def save_bootstrap_config(config: DatabaseBootstrapConfig) -> None:
    path = _bootstrap_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid4()}.tmp")
    try:
        temporary.write_text(config.model_dump_json(by_alias=True, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
