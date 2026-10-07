#!/usr/bin/env python3
"""Export the FastAPI OpenAPI schema used by the Fern docs API reference.

Run with the backend virtualenv active (``source .venv/bin/activate``):

    python scripts/export-openapi.py

The generated file (``docs/fern/apis/python/openapi.json``) is committed so the
docs site can be previewed and checked without the Python backend installed.
Regenerate it whenever backend routes or schemas change.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "docs" / "fern" / "apis" / "python" / "openapi.json"
METHODS = {"get", "post", "put", "patch", "delete"}


def main() -> int:
    from llm_d_bench.api.main import app

    spec = app.openapi()
    paths = spec.get("paths", {})
    operations = sum(1 for item in paths.values() for method in item if method in METHODS)
    OUTPUT.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(paths)} paths, {operations} operations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
