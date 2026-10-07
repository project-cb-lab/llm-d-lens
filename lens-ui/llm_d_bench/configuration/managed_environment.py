"""Policy for Hugging Face environment variables on model-serving Pods.

Hugging Face endpoints, tokens, cache locations and offline switches are
admin-managed: the platform injects them from the cluster's saved proxy, model
cache and token configuration (see ``deploy.runtime.composition._model_environment``
and ``deploy.providers.model_cache_environment``). A user-supplied runtime
override must not be able to replace them, so the deployment APIs reject such
names.
"""

ADMIN_MANAGED_ENVIRONMENT_PREFIXES = ("HF_", "HUGGING", "TRANSFORMERS_")


def is_admin_managed_environment_variable(name: object) -> bool:
    """Return whether ``name`` is a Hugging Face variable the platform manages."""
    normalized = str(name or "").strip().upper()
    return bool(normalized) and normalized.startswith(ADMIN_MANAGED_ENVIRONMENT_PREFIXES)


def admin_managed_environment_names(custom_parameters: object) -> list[str]:
    """Return the admin-managed environment names declared in runtime overrides."""
    if not isinstance(custom_parameters, list):
        return []
    names = []
    for item in custom_parameters:
        if not isinstance(item, dict) or item.get("kind") != "environment":
            continue
        name = item.get("name")
        if is_admin_managed_environment_variable(name):
            names.append(str(name))
    return names


def admin_managed_environment_error(name: str) -> str:
    return f"Environment variable {name} is managed by the platform administrator and cannot be set here."
