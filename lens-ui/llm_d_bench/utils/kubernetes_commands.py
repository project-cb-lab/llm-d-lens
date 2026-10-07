"""Finite compatibility adapter for existing Kubernetes argv-based callers.

Only recognized command shapes are executed. None means choose CLI *before*
effects; failed SDK operations always return failure and must never be retried
through CLI. Domain validation still runs before this transport boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import yaml
from aiohttp import ClientError
from kubernetes.aio.client.exceptions import ApiException

from llm_d_bench.utils import kubernetes_api as api
from llm_d_bench.utils import kubernetes_mutations as mutations
from llm_d_bench.utils.kubernetes_auth import CliAuthenticationRequiredError
from llm_d_bench.utils.shell import CommandResult

_OPTIONS = {
    "-n": "namespace",
    "--namespace": "namespace",
    "-o": "output",
    "--output": "output",
    "-l": "selector",
    "--selector": "selector",
    "--field-selector": "field_selector",
    "--type": "patch_type",
    "-p": "patch",
    "--patch": "patch",
    "--replicas": "replicas",
    "--kubeconfig": "kubeconfig",
    "--raw": "raw",
    "--timeout": "timeout",
    "--request-timeout": "request_timeout",
    "--cascade": "cascade",
    "--tail": "tail",
    "-c": "container",
    "--container": "container",
    "-f": "filename",
    "--filename": "filename",
}
_BOOLS = {
    "-A": "all_namespaces",
    "--all-namespaces": "all_namespaces",
    "--ignore-not-found": "ignore_not_found",
    "--wait": "wait",
    "--overwrite": "overwrite",
    "--all": "all",
    "--previous": "previous",
    "--timestamps": "timestamps",
}


def sdk_enabled() -> bool:
    value = os.environ.get("PRISM_KUBERNETES_BACKEND", os.environ.get("PRISM_KUBERNETES_READ_BACKEND", "sdk"))
    if value.strip().lower() not in {"sdk", "cli"}:
        raise ValueError("PRISM_KUBERNETES_BACKEND / PRISM_KUBERNETES_READ_BACKEND must be cli or sdk")
    return value.strip().lower() == "sdk"


def sdk_writes_enabled() -> bool:
    """The legacy READ_BACKEND flag must not silently enable SDK mutations."""
    return sdk_enabled() and (
        "PRISM_KUBERNETES_BACKEND" in os.environ or "PRISM_KUBERNETES_READ_BACKEND" not in os.environ
    )


def _parse(argv):
    positional, options = [], {}
    tokens = iter(argv[1:])
    for token in tokens:
        key, sep, value = token.partition("=")
        if key in _OPTIONS:
            value = value if sep else next(tokens, None)
            if value is None or _OPTIONS[key] in options:
                return None
            options[_OPTIONS[key]] = value
        elif key in _BOOLS:
            if _BOOLS[key] in options or (sep and value not in {"true", "false"}):
                return None
            options[_BOOLS[key]] = value != "false"
        elif token.startswith("-"):
            return None
        else:
            positional.append(token)
    return positional, options


def _seconds(value):
    if value is None:
        return None
    if value == "0":
        return 0
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
    if not parts or "".join(number + unit for number, unit in parts) != value:
        raise ValueError("Unsupported Kubernetes duration")
    return sum(float(number) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit] for number, unit in parts)


def _object_name(resource, name):
    if "/" in resource:
        if name:
            raise ValueError("Ambiguous Kubernetes object name")
        return resource.split("/", 1)
    return resource, name


async def _execute(pos, opts, kubeconfig, timeout):
    verb, *args = pos
    base = {"kubeconfig": kubeconfig, "namespace": opts.get("namespace"), "timeout": timeout}
    if verb == "version":
        return {"serverVersion": await api.server_version(kubeconfig=kubeconfig)}
    if verb == "config":
        async with api.api_session(kubeconfig) as (_, context):
            return context["name"]
    if verb == "get" and "raw" in opts:
        async with api.api_session(kubeconfig) as (connection, _):
            path = opts["raw"]
            if path == "/readyz":
                return await connection.call_api(
                    path,
                    "GET",
                    response_types_map={200: "str"},
                    auth_settings=["BearerToken"],
                    _return_http_data_only=True,
                    _request_timeout=(5, timeout),
                )
            return await api.request(connection, path, timeout=timeout)
    if verb == "cluster-info":
        await api.server_version(kubeconfig=kubeconfig)
        return "Kubernetes API is reachable"
    if verb == "get":
        resource, name = _object_name(args[0], args[1] if len(args) > 1 else None)
        results = []
        for kind in resource.split(","):
            try:
                result = await api.query_resource(
                    kind,
                    name=name,
                    all_namespaces=opts.get("all_namespaces", False),
                    selector=opts.get("selector"),
                    field_selector=opts.get("field_selector"),
                    **base,
                )
            except ApiException as error:
                if error.status == 404 and opts.get("ignore_not_found"):
                    result = {"items": []}
                else:
                    raise
            results.append(result)
        if len(results) == 1:
            return results[0]
        return {
            "apiVersion": "v1",
            "kind": "List",
            "items": [item for result in results for item in result.get("items", [])],
        }
    if verb == "create":
        if "_manifest" in opts:
            body = opts["_manifest"]
            base["namespace"] = opts.get("namespace") or body.get("metadata", {}).get("namespace")
            return await mutations.create_sdk_resource(body["kind"].lower(), body, **base)
        return await mutations.create_sdk_resource(
            "namespaces", {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": args[1]}}, **base
        )
    if verb in {"cordon", "uncordon"}:
        return await mutations.patch_sdk_resource(
            "nodes", args[0], {"spec": {"unschedulable": verb == "cordon"}}, **base
        )
    resource, name = _object_name(args[0], None if "/" in args[0] else (args[1] if len(args) > 1 else None))
    if verb == "patch":
        return await mutations.patch_sdk_resource(
            resource, name, json.loads(opts["patch"]), patch_type=opts.get("patch_type", "strategic"), **base
        )
    if verb == "scale":
        return await mutations.scale_sdk_resource(resource, name, int(opts["replicas"]), **base)
    if verb == "label":
        labels = {}
        start = 1 if "/" in args[0] else 2
        for token in args[start:]:
            if "=" in token:
                key, value = token.split("=", 1)
                labels[key] = value
            elif token.endswith("-"):
                labels[token[:-1]] = None
        return await mutations.label_sdk_resource(
            resource, name, labels, overwrite=opts.get("overwrite", False), **base
        )
    if verb == "delete":
        options = {
            **base,
            "wait": opts.get("wait", True),
            "ignore_not_found": opts.get("ignore_not_found", False),
            "propagation_policy": {"background": "Background", "foreground": "Foreground", "orphan": "Orphan"}[
                opts.get("cascade", "background")
            ],
        }
        if name:
            return await mutations.delete_sdk_resource(resource, name, **options)
        return await mutations.delete_sdk_resources(
            resource, selector=opts.get("selector"), all_namespaces=opts.get("all_namespaces", False), **options
        )
    if verb == "logs":
        async with api.api_session(kubeconfig) as (connection, context):
            ns = api.selected_namespace(context, opts.get("namespace"))
            descriptor = await api.resolve_resource(connection, "pods")
            query = [
                (key, opts[source])
                for source, key in (("container", "container"), ("tail", "tailLines"))
                if source in opts
            ]
            query += [(key, str(opts[key]).lower()) for key in ("previous", "timestamps") if key in opts]
            return await connection.call_api(
                descriptor.path(ns, args[0].removeprefix("pod/").removeprefix("pods/")) + "/log",
                "GET",
                query_params=query,
                response_types_map={200: "str"},
                auth_settings=["BearerToken"],
                _return_http_data_only=True,
                _request_timeout=(5, timeout),
            )
    raise ValueError("Unsupported Kubernetes operation")


def _supported(pos, opts):
    if not pos:
        return False
    verb, *args = pos
    common = {"namespace", "kubeconfig", "timeout", "request_timeout"}
    allowed = {
        "get": {"output", "selector", "field_selector", "all_namespaces", "ignore_not_found", "raw"},
        "version": {"output"},
        "config": set(),
        "cluster-info": set(),
        "create": {"output", "filename"},
        "patch": {"patch", "patch_type", "output"},
        "scale": {"replicas", "output"},
        "delete": {"ignore_not_found", "wait", "cascade", "selector", "all_namespaces", "all", "output"},
        "label": {"overwrite", "output"},
        "cordon": set(),
        "uncordon": set(),
        "logs": {"container", "tail", "previous", "timestamps"},
    }
    if verb not in allowed or set(opts) - common - allowed[verb]:
        return False
    # Request timeout is per HTTP request in kubectl, not a whole-command deadline.
    # Leave it to CLI until that distinction is implemented explicitly.
    if "request_timeout" in opts or ("timeout" in opts and verb != "delete"):
        return False
    try:
        _seconds(opts.get("timeout"))
    except ValueError:
        return False
    if "output" in opts and opts["output"] not in {"json", "yaml", "name"}:
        return False
    if verb == "version":
        return not args and opts.get("output") == "json"
    if verb == "config":
        return args == ["current-context"]
    if verb == "cluster-info":
        return not args
    if verb == "get":
        if "raw" in opts:
            return (
                not args
                and not (set(opts) & {"selector", "field_selector", "all_namespaces", "ignore_not_found", "output"})
                and opts["raw"].startswith(("/api/", "/apis/", "/readyz", "/version"))
                and ".." not in opts["raw"]
            )
        if not 1 <= len(args) <= 2 or opts.get("output") not in {"json", "yaml", "name"}:
            return False
        named = len(args) == 2 or "/" in args[0]
        if "/" in args[0] and (len(args) != 1 or args[0].count("/") != 1 or not all(args[0].split("/"))):
            return False
        return not (
            named
            and ("," in args[0] or opts.get("selector") or opts.get("field_selector") or opts.get("all_namespaces"))
        )
    if verb == "create":
        if "filename" in opts:
            return not args and opts["filename"] == "-"
        return len(args) == 2 and args[0] in {"namespace", "namespaces", "ns"}
    if verb in {"cordon", "uncordon", "logs"}:
        if len(args) != 1:
            return False
        if verb == "logs":
            return "/" not in args[0] or (
                args[0].count("/") == 1 and args[0].split("/")[0] in {"pod", "pods"} and bool(args[0].split("/")[1])
            )
        return "/" not in args[0]
    combined_name = bool(args and args[0].count("/") == 1 and all(args[0].split("/")))
    has_name = (len(args) == 2 and "/" not in args[0]) or (len(args) == 1 and combined_name)
    if args and "," in args[0]:
        return False
    if verb == "patch":
        return has_name and "patch" in opts and opts.get("patch_type", "strategic") in {"merge", "strategic", "json"}
    if verb == "scale":
        return has_name and "replicas" in opts and opts["replicas"].isdigit()
    if verb == "label":
        start = 1 if combined_name else 2
        return (
            len(args) > start
            and (combined_name or "/" not in args[0])
            and all(
                ("=" in token and bool(token.split("=", 1)[0])) or (token.endswith("-") and len(token) > 1)
                for token in args[start:]
            )
        )
    if verb == "delete":
        if "output" in opts:
            return False
        if has_name and (opts.get("selector") or opts.get("all") or opts.get("all_namespaces")):
            return False
        if opts.get("all") and opts.get("selector"):
            return False
        return (
            (has_name or (len(args) == 1 and (opts.get("selector") or opts.get("all"))))
            and opts.get("cascade", "background") in {"background", "foreground", "orphan"}
            and "," not in args[0]
        )
    return False


async def execute_sdk_command(
    argv, *, kubeconfig=None, timeout=30, stdin_data=None, allow_writes=True
) -> CommandResult | None:
    if not argv or Path(argv[0]).name != "kubectl":
        return None
    parsed = _parse(argv)
    if parsed is None or not _supported(*parsed):
        return None
    pos, opts = parsed
    if not allow_writes and pos[0] not in {"get", "version", "config", "cluster-info", "logs"}:
        return None
    if stdin_data is not None:
        if pos != ["create"] or opts.get("filename") != "-":
            return None
        try:
            manifest = yaml.safe_load(stdin_data)
        except yaml.YAMLError:
            return None
        versions = {
            "v1": {"Secret", "ConfigMap", "Pod", "Service", "Namespace", "PersistentVolumeClaim", "PersistentVolume"},
            "apps/v1": {"Deployment", "StatefulSet", "DaemonSet"},
            "batch/v1": {"Job", "CronJob"},
        }
        if (
            not isinstance(manifest, dict)
            or not isinstance(manifest.get("apiVersion"), str)
            or not isinstance(manifest.get("kind"), str)
            or manifest.get("kind") not in versions.get(manifest.get("apiVersion"), set())
        ):
            return None
        metadata = manifest.get("metadata") or {}
        if not isinstance(metadata, dict):
            return None
        if opts.get("namespace") and metadata.get("namespace") and opts["namespace"] != metadata["namespace"]:
            return None
        opts["_manifest"] = manifest
    elif opts.get("filename"):
        return None
    budget = min(
        value
        for value in (timeout or 30, _seconds(opts.get("timeout")), _seconds(opts.get("request_timeout")))
        if value is not None and value > 0
    )
    try:
        async with asyncio.timeout(budget):
            result = await _execute(pos, opts, opts.get("kubeconfig", kubeconfig), budget)
        output = opts.get("output")
        if isinstance(result, str):
            stdout = result
        elif not output and pos[0] in {"create", "patch", "label", "scale", "delete", "cordon", "uncordon"}:
            # kubectl's default mutation output is a status line, not an object
            # dump. In particular, never expose Secret data through job logs.
            objects = result if isinstance(result, list) else [result]
            stdout = "\n".join(
                f"{item.get('kind', 'resource').lower()}/{item.get('metadata', {}).get('name', '')} {pos[0]} succeeded"
                for item in objects
                if isinstance(item, dict)
            )
        elif output == "yaml":
            stdout = yaml.safe_dump(result)
        elif output == "name":
            objects = result.get("items", []) if "items" in result else [result]
            fallback_kind = pos[1].split("/")[0] if len(pos) > 1 else "resource"
            stdout = "\n".join(
                f"{item.get('kind', fallback_kind).lower()}/{item.get('metadata', {}).get('name', '')}"
                for item in objects
            )
        else:
            stdout = json.dumps(result)
        return CommandResult(tuple(argv), 0, stdout, "")
    except CliAuthenticationRequiredError:
        return None
    except ApiException as error:
        reason = {
            400: "BadRequest",
            401: "Unauthorized",
            403: "Forbidden",
            404: "NotFound",
            405: "MethodNotAllowed",
            409: "Conflict",
            422: "Invalid",
            429: "TooManyRequests",
            500: "InternalError",
            503: "ServiceUnavailable",
        }.get(error.status, "Unknown")
        # Only canonical Status.reason values are safe; never echo API bodies/messages.
        try:
            reported = json.loads(error.body or "{}").get("reason")
        except (ValueError, AttributeError):
            reported = None
        if isinstance(reported, str) and reported in {
            "AlreadyExists",
            "NotFound",
            "Conflict",
            "Forbidden",
            "Unauthorized",
            "Invalid",
        }:
            reason = reported
        elif error.status == 409 and pos[0] == "create":
            reason = "AlreadyExists"
        return CommandResult(tuple(argv), 1, "", f"Kubernetes {pos[0]} failed ({reason}, HTTP {error.status})")
    except TimeoutError:
        return CommandResult(tuple(argv), 124, "", f"Kubernetes {pos[0]} timed out")
    except (ClientError, ValueError) as error:
        return CommandResult(tuple(argv), 1, "", f"Kubernetes {pos[0]} failed ({type(error).__name__})")
