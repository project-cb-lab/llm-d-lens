"""Keep YAML-only edits from invalidating the structured configuration facts."""

from copy import deepcopy

import yaml


def validate_manifest_edit(original: str, edited: str) -> None:
    """Allow formatting and resource annotations; runtime edits use the typed editor.

    Arbitrary shell scripts and Guide-specific resources cannot be losslessly
    reverse-engineered into Configuration inputs. Reject unsupported edits
    instead of attaching stale model/topology/runtime facts to new YAML.
    """

    def resources(content: str) -> dict:
        result = {}
        for document in yaml.safe_load_all(content):
            if document is None:
                continue
            if not isinstance(document, dict):
                raise ValueError("Configuration YAML must contain resource mappings")
            item = deepcopy(document)
            metadata = item.get("metadata") or {}
            key = (item.get("apiVersion"), item.get("kind"), metadata.get("name"))
            if key in result:
                raise ValueError("Configuration YAML contains duplicate resources")
            metadata.pop("annotations", None)
            result[key] = item
        return result

    if resources(original) != resources(edited):
        raise ValueError(
            "YAML editing supports resource annotations and formatting only. "
            "Change model, topology, runtime arguments, environment or storage in the configuration editor "
            "and generate YAML again so the saved configuration facts remain accurate."
        )
