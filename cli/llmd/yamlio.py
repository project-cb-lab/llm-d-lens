"""YAML/JSON helpers: strict parsing, reviewable output, stable digests, atomic writes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .errors import UsageError


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys; silently keeping the last one hides mistakes."""


def _construct_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise UsageError(f"duplicate YAML key {key!r} (line {key_node.start_mark.line + 1})")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


class _ReviewableDumper(yaml.SafeDumper):
    pass


def _represent_str(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_ReviewableDumper.add_representer(str, _represent_str)


def load_documents(text: str, origin: str = "<input>") -> list[dict[str, Any]]:
    """Parse a multi-document stream, expanding ``kind: List`` and dropping empty documents."""
    try:
        raw = [doc for doc in yaml.load_all(text, Loader=_UniqueKeyLoader) if doc is not None]
    except yaml.YAMLError as exc:
        raise UsageError(f"{origin}: invalid YAML: {exc}") from exc
    documents: list[dict[str, Any]] = []
    for doc in raw:
        if not isinstance(doc, dict):
            raise UsageError(f"{origin}: every YAML document must be a mapping")
        if doc.get("kind") == "List" and isinstance(doc.get("items"), list):
            documents.extend(item for item in doc["items"] if isinstance(item, dict))
        else:
            documents.append(doc)
    return documents


def load_file(path: Path) -> Any:
    try:
        return yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise UsageError(f"{path}: invalid YAML: {exc}") from exc


def dump(value: Any) -> str:
    return yaml.dump(value, Dumper=_ReviewableDumper, sort_keys=False, allow_unicode=True)


def dump_documents(documents: list[dict[str, Any]]) -> str:
    return yaml.dump_all(documents, Dumper=_ReviewableDumper, sort_keys=False, allow_unicode=True)


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def write_text_atomic(path: Path, text: str, *, mode: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json_atomic(path: Path, value: Any) -> None:
    write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def kubectl_objects(text: str) -> list[dict[str, Any]]:
    """The objects in ``kubectl ... -o json`` output. Commands that act on several objects
    (``create -f``, ``apply -f``) print one JSON document per object rather than a List, depending
    on the kubectl version; a ``List`` is unwrapped."""
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    position, text = 0, text.strip()
    while position < len(text):
        value, end = decoder.raw_decode(text, position)
        if isinstance(value, dict) and str(value.get("kind", "")).endswith("List") and "items" in value:
            objects.extend(item for item in value["items"] or [] if isinstance(item, dict))
        elif isinstance(value, dict):
            objects.append(value)
        position = end
        while position < len(text) and text[position].isspace():
            position += 1
    return objects


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default
