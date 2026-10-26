"""Render a stack to Kubernetes objects on this machine. Rendering never contacts the cluster.

A kustomize stack renders with ``kustomize build`` (``kubectl kustomize`` when kustomize is
not installed), with ``--enable-helm`` when the stack includes a Helm chart. A manifest
stack is its files, read as they are. Either way the result is exactly what the team's
own ``oc apply -k`` or ``oc apply -f`` would send.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import yamlio
from .catalog import Stack, manifest_files, uses_helm
from .errors import CommandError
from .process import Runner, available, display


@dataclass(frozen=True)
class Tools:
    kube: str = "kubectl"
    kustomize: str = "kustomize"

    def kustomize_command(self) -> list[str]:
        if self.kustomize and available(self.kustomize):
            return [self.kustomize, "build"]
        return [self.kube, "kustomize"]


@dataclass
class Rendered:
    documents: list[dict[str, Any]]
    #: The command (or the files read) that produced the documents, for the plan's evidence.
    command: str


def _short(message: str, limit: int = 1200) -> str:
    """Tool errors can be pages long; keep the lines that explain them."""
    if len(message) <= limit:
        return message
    errors = [line for line in message.splitlines() if line.lower().startswith("error")]
    text = "\n".join(errors) if errors else message
    return text if len(text) <= limit else "..." + text[-limit:]


def render(stack: Stack, *, runner: Runner, tools: Tools | None = None) -> Rendered:
    tools = tools or Tools()
    if stack.kind == "manifests":
        files = manifest_files(stack.path)
        documents = []
        for path in files:
            documents.extend(yamlio.load_documents(path.read_text(encoding="utf-8"), str(path)))
        return Rendered(documents, f"read {len(files)} manifest file(s) under {stack.id}")
    directory = stack.path
    command = tools.kustomize_command()
    options = ["--enable-helm"] if uses_helm(directory) else []
    result = runner([*command, str(directory), *options], timeout=300, cwd=str(directory))
    if not result.ok:
        raise CommandError(f"kustomize render of {stack.id} failed: {_short(result.message)}")
    documents = yamlio.load_documents(result.stdout, stack.id)
    return Rendered(documents, display([Path(command[0]).name, *command[1:], stack.id, *options]))
