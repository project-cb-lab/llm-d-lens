"""A plan: what a stack renders to in one namespace, and every finding about it.

A plan is the same object offline (no cluster) and against a cluster; cluster checks add
findings to it (``preflight``). Its digest covers what would be applied, so ``standup
--expected-digest`` refuses to apply anything other than the plan that was reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import yamlio
from .catalog import Stack
from .model import Finding, object_ref, validate_instance_name, validate_namespace
from .policy import PolicyContext, PolicyResult, ScopeResolver, apply_policy, fallback_scope
from .process import Runner
from .render import Tools, render


@dataclass
class Options:
    """What a user may change about a plan; everything else comes from the stack's files."""

    namespace: str
    instance: str
    allow_privileged: bool = False
    skip_admin_parts: bool = False
    #: Let the instance move to this stack path from the one it runs (the objects stay where they are).
    switch_stack: bool = False


@dataclass
class Plan:
    stack: Stack
    options: Options
    command: str
    policy: PolicyResult
    #: Results of the checks against the cluster, when there was one.
    cluster: dict[str, Any] = field(default_factory=dict)

    @property
    def namespace(self) -> str:
        return self.options.namespace

    @property
    def instance(self) -> str:
        return self.options.instance

    @property
    def documents(self) -> list[dict[str, Any]]:
        return self.policy.documents

    @property
    def findings(self) -> list[Finding]:
        return self.policy.findings

    @property
    def blocking(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "error"]

    @property
    def digest(self) -> str:
        return yamlio.digest(
            {"namespace": self.namespace, "instance": self.instance, "documents": self.documents}
        )

    def objects(self) -> list[dict[str, str]]:
        return [
            {"apiVersion": doc["apiVersion"], "kind": doc["kind"], "name": doc["metadata"]["name"]}
            for doc in self.documents
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "stack": self.stack.id,
            "source": self.stack.source.describe(),
            "namespace": self.namespace,
            "instance": self.instance,
            "render": self.command,
            "objects": [object_ref(doc) for doc in self.documents],
            "workloads": self.policy.workloads,
            "accelerators": self.policy.accelerators,
            "prerequisites": self.policy.prerequisites,
            "adminParts": [
                {"object": object_ref(part["document"]), "reason": part["reason"]}
                for part in self.policy.admin_parts
            ],
            "findings": [finding.as_dict() for finding in self.findings],
            "cluster": self.cluster,
            "digest": self.digest,
            "blocking": len(self.blocking),
        }


def make_plan(
    stack: Stack,
    options: Options,
    *,
    runner: Runner,
    tools: Tools | None = None,
    scope: ScopeResolver = fallback_scope,
    label: bool = True,
) -> Plan:
    """Render the stack and apply the namespace policy. Never contacts the cluster."""
    validate_namespace(options.namespace)
    validate_instance_name(options.instance)
    rendered = render(stack, runner=runner, tools=tools)
    context = PolicyContext(
        namespace=options.namespace,
        instance=options.instance,
        allow_privileged=options.allow_privileged,
        skip_admin_parts=options.skip_admin_parts,
        label=label,
        scope=scope,
    )
    return Plan(
        stack=stack,
        options=options,
        command=rendered.command,
        policy=apply_policy(rendered.documents, context),
    )
