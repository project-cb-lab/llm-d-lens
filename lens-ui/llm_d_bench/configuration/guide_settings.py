"""Check guide-specific settings against the resources that will be deployed."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

import yaml

from .manifest_facts import _invocation_tokens, _option

DEFAULT_RDMA_DEVICE_CLASS = "dranet-rdma"


def _rdma_device_class() -> str:
    """RDMA NIC device class, from the active deploy hardware profile."""
    try:
        from llm_d_bench.hardware.resolver import resolve_by_accelerator_key

        profile = resolve_by_accelerator_key("xpu")
        value = profile.dranet_device_class if profile else None
    except Exception:  # pragma: no cover - discovery failure keeps the literal fallback
        value = None
    return value or DEFAULT_RDMA_DEVICE_CLASS


def _mapping(text: str) -> dict:
    value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError("Router values must be a YAML mapping")
    return value


def _merge(base: dict, overlay: dict) -> dict:
    for key, value in overlay.items():
        if key in {"__proto__", "constructor", "prototype"}:
            raise ValueError("Invalid router values key")
        base[key] = (
            _merge(base[key], value) if isinstance(base.get(key), dict) and isinstance(value, dict) else deepcopy(value)
        )
    return base


def validate_guide_settings(content: dict[str, Any], manifest: str, guide: str) -> None:
    settings = content.get("guideSettings", {})
    if settings is None:
        settings = {}
    if not isinstance(settings, dict) or set(settings) - {"cacheCpuGiB", "rdmaNicCount", "routerValues"}:
        raise ValueError("Unknown Guide settings")
    for key in ("cacheCpuGiB", "rdmaNicCount"):
        value = settings.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            or (key == "rdmaNicCount" and int(value) != value)
        ):
            raise ValueError(f"Invalid Guide setting {key}")
    variant = content.get("guideVariant", "")
    capacity = settings.get("cacheCpuGiB")
    nic_count = settings.get("rdmaNicCount")
    if capacity is not None and (
        guide != "tiered-prefix-cache" or variant not in {"native/cpu/base", "lmcache-connector/cpu/base"}
    ):
        raise ValueError("CPU cache capacity requires a supported offload variant")
    if nic_count is not None and (guide != "pd-disaggregation" or variant != "vllm-rdma"):
        raise ValueError("NIC count requires the P/D RDMA variant")
    documents = [doc for doc in yaml.safe_load_all(manifest) if isinstance(doc, dict)]
    servers = [
        container
        for doc in documents
        if doc.get("kind") == "Deployment"
        for container in doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        if container.get("name") == "modelserver"
    ]
    if capacity is not None:
        if not servers:
            raise ValueError("CPU cache settings require model servers")
        for container in servers:
            tokens = _invocation_tokens(container, "cache")
            connector = json.loads(_option(tokens, {"--kv-transfer-config"}, "cache") or "{}")
            if variant == "native/cpu/base":
                if connector.get("kv_connector") != "OffloadingConnector" or connector.get(
                    "kv_connector_extra_config", {}
                ).get("cpu_bytes_to_use") != int(capacity * 1024**3):
                    raise ValueError("CPU cache capacity does not match the native connector")
            else:
                environment = {entry.get("name"): entry.get("value") for entry in container.get("env", [])}
                if (
                    not str(connector.get("kv_connector", "")).startswith("LMCacheConnector")
                    or "LMCACHE_CONFIG_FILE" in environment
                    or float(environment.get("LMCACHE_MAX_LOCAL_CPU_SIZE") or 0) != capacity
                ):
                    raise ValueError("CPU cache capacity does not match LMCache configuration")
    if nic_count is not None:
        requests = [
            request
            for doc in documents
            if doc.get("kind") == "ResourceClaimTemplate"
            for request in doc.get("spec", {}).get("spec", {}).get("devices", {}).get("requests", [])
        if request.get("exactly", {}).get("deviceClassName") == _rdma_device_class()
        ]
        if not requests or any(request["exactly"].get("count") != nic_count for request in requests):
            raise ValueError("NIC count does not match the rendered RDMA requests")
    router_values = settings.get("routerValues", "")
    if not isinstance(router_values, str):
        raise ValueError("Router values must be YAML text")
    bundle = (content.get("officialGuide") or {}).get("deploymentBundle")
    if not bundle:
        if router_values.strip():
            raise ValueError("Router settings require a saved deployment bundle")
        return
    layers = {asset["name"]: asset["content"] for asset in bundle["helm"]["values"]}
    if not {"router-base.yaml", "router-guide.yaml", "router-effective.yaml"}.issubset(layers):
        raise ValueError("Deployment bundle requires original and effective router values")
    expected = _merge(_mapping(layers["router-base.yaml"]), _mapping(layers["router-guide.yaml"]))
    if router_values.strip():
        _merge(expected, _mapping(router_values))
    if guide == "precise-prefix-cache-routing":
        sizes = {
            _option(_invocation_tokens(container, "precise"), {"--block-size"}, "precise") for container in servers
        }
        if not servers or len(sizes) != 1 or None in sizes:
            raise ValueError("Precise routing requires one consistent model-server block size")
        size = int(next(iter(sizes)))
        try:
            epp = expected["router"]["epp"]
            key = epp["pluginsConfigFile"]
            plugins = _mapping(epp["pluginsCustomConfig"][key])
            token = next(item for item in plugins["plugins"] if item["type"] == "token-producer")
            index = next(
                item
                for item in plugins["plugins"]
                if item["type"] in {"precise-prefix-cache-producer", "precise-prefix-cache-scorer"}
            )
            token.setdefault("parameters", {})["modelName"] = content["model"]["name"]
            index.setdefault("parameters", {}).setdefault("tokenProcessorConfig", {})["blockSizeTokens"] = size
            epp["pluginsCustomConfig"][key] = yaml.safe_dump(plugins)
            if epp.get("replicas", 1) != 1:
                raise ValueError("Precise token-load routing requires one EPP replica")
        except (KeyError, StopIteration, TypeError) as error:
            raise ValueError("Precise router plugins are incomplete") from error
    effective = _mapping(layers["router-effective.yaml"])
    # Nested plugin YAML may be serialized differently by js-yaml and PyYAML.
    for values in (expected, effective):
        custom = values.get("router", {}).get("epp", {}).get("pluginsCustomConfig", {})
        for key, value in custom.items():
            if isinstance(value, str):
                custom[key] = yaml.safe_load(value)
    if effective != expected:
        raise ValueError("Effective router values do not match the saved Guide settings and model configuration")


def validate_configuration_extensions(content: dict[str, Any], manifest: str) -> None:
    official = content.get("officialGuide") or {}
    source = official.get("source") or {}
    bundle = official.get("deploymentBundle")
    if bundle is not None:
        from llm_d_bench.deploy.providers.deployment_bundle import validate_deployment_bundle

        validate_deployment_bundle(bundle, source.get("guide"), source.get("commit"))
    validate_guide_settings(content, manifest, source.get("guide", ""))
