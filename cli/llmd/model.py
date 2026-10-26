"""Value types and names shared by the planner, the inventory, and the commands."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .errors import UsageError

#: On objects llmd creates (never on pod templates, and never on imported objects).
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "llm-d-lens"
INSTANCE_LABEL = "lens.llm-d.ai/instance"
FIELD_MANAGER = "llm-d-lens"

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
#: Short enough that generated names (``lens-instance-<name>``) stay valid.
MAX_INSTANCE_LENGTH = 40


def is_dns_label(value: object, max_length: int = 63) -> bool:
    return isinstance(value, str) and len(value) <= max_length and bool(_DNS_LABEL.match(value))


def validate_instance_name(value: str) -> str:
    if not is_dns_label(value, MAX_INSTANCE_LENGTH):
        raise UsageError(
            f"invalid instance name {value!r}",
            hint=f"Use lowercase letters, digits, and '-' (at most {MAX_INSTANCE_LENGTH} characters).",
        )
    return value


def validate_namespace(value: str | None) -> str:
    if not value:
        raise UsageError(
            "no target namespace",
            hint="Pass -n/--namespace, set LLMD_NAMESPACE, or select a namespace in your kube context.",
        )
    if not is_dns_label(value):
        raise UsageError(f"invalid namespace {value!r}")
    return value


@dataclass(frozen=True)
class Finding:
    """A planner observation. ``error`` findings block a standup unless acknowledged."""

    severity: str  # error | warning | info
    code: str
    message: str
    object: str = ""
    hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


def object_ref(document: dict[str, Any]) -> str:
    return f"{document.get('kind', '?')}/{(document.get('metadata') or {}).get('name', '?')}"


def api_group(api_version: str) -> str:
    return api_version.split("/", 1)[0] if "/" in api_version else ""
