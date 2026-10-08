"""Small, shared filesystem and process boundary helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

import yaml

VERSION_LABEL = "rollouts.llm-d.ai/version"
ROLE_LABEL = "rollouts.llm-d.ai/role"


class RolloutError(Exception):
    """An actionable input, validation, or external-command failure."""


def require(condition, message):
    if not condition:
        raise RolloutError(message)


def run(argv, *, cwd=None, input=None, timeout=180):
    args = list(map(str, argv))
    try:
        result = subprocess.run(
            args, cwd=cwd, input=input, text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RolloutError(f"{shlex.join(args)}: {exc}") from exc
    require(result.returncode == 0, f"{shlex.join(args)}:\n{result.stderr or result.stdout}")
    return result.stdout


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        require(isinstance(key, str), "YAML mapping keys must be strings")
        require(key not in result, f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def documents(text):
    try:
        docs = [doc for doc in yaml.load_all(text, Loader=UniqueLoader) if doc is not None]
    except yaml.YAMLError as exc:
        raise RolloutError(f"invalid YAML: {exc}") from exc
    require(all(isinstance(doc, dict) for doc in docs), "expected YAML document mappings")
    return docs


def read(path):
    docs = documents(Path(path).read_text())
    require(len(docs) == 1, f"{path}: expected exactly one YAML document")
    return docs[0]


class ReviewableDumper(yaml.SafeDumper):
    pass


def _string(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


ReviewableDumper.add_representer(str, _string)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".rollout-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            yaml.dump(value, stream, Dumper=ReviewableDumper, sort_keys=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise RolloutError(f"content must contain finite JSON-compatible YAML values: {exc}") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


def local_path(root, value):
    require(isinstance(value, str) and value and not Path(value).is_absolute(), "expected a relative path")
    root = Path(root).resolve()
    path = root / value
    require(path.resolve().is_relative_to(root), f"path escapes the environment: {value}")
    require(path.is_file() and not path.is_symlink(), f"missing regular file: {path}")
    return path


def segment(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value),
        f"invalid model/environment path segment: {value!r}",
    )
    return value


def dns_label(value):
    return (
        isinstance(value, str)
        and len(value) <= 63
        and re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value)
    )
