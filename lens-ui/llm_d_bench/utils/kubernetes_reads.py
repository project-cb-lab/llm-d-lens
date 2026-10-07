"""Compatibility list facade over strict raw SDK resource operations."""

from __future__ import annotations

import logging
import re

from aiohttp import ClientError
from kubernetes.aio.client.exceptions import ApiException

from llm_d_bench.utils.kubernetes_api import query_resource
from llm_d_bench.utils.kubernetes_auth import CliAuthenticationRequiredError as CliAuthenticationRequiredError

logger = logging.getLogger(__name__)
_READ_TIMEOUT = 15.0


def supports_resource(resource: str) -> bool:
    """Resource names and group-qualified CRDs; command expressions stay outside."""
    return bool(re.fullmatch(r"[a-z][a-z0-9.-]*", resource))


async def list_sdk_resources(
    resource: str, *, kubeconfig: str | None, namespace: str | None, selector: str | None, all_namespaces: bool
) -> list[dict]:
    """Preserve legacy empty-list errors; strict callers use query_resource."""
    try:
        result = await query_resource(
            resource,
            kubeconfig=kubeconfig,
            namespace=namespace,
            selector=selector,
            all_namespaces=all_namespaces,
            timeout=_READ_TIMEOUT,
        )
        return result["items"]
    except ApiException as error:
        logger.warning("Kubernetes SDK list failed: resource=%s category=api status=%s", resource, error.status)
    except TimeoutError:
        logger.warning("Kubernetes SDK list failed: resource=%s category=timeout", resource)
    except ClientError:
        logger.warning("Kubernetes SDK list failed: resource=%s category=transport", resource)
    except ValueError:
        logger.warning("Kubernetes SDK list failed: resource=%s category=response", resource)
    return []
