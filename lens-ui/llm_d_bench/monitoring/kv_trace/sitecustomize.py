"""Install the Prism probe only after vLLM imports its KV manager normally."""

import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys


class _ProbeFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "vllm.v1.core.kv_cache_manager":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        original = spec.loader

        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return original.create_module(spec)

            def exec_module(self, module):
                original.exec_module(module)
                try:
                    from prism_kv_engine import install

                    install(module)
                except Exception:  # noqa: S110 - failed instrumentation leaves collection unavailable
                    pass  # No ready marker means collection is unavailable.

        spec.loader = Loader()
        return spec


if os.environ.get("PRISM_KV_TRACE_NAMESPACE"):
    sys.meta_path.insert(0, _ProbeFinder())

# Preserve image-specific Python startup hooks (notably accelerator images).
_own_directory = os.path.dirname(os.path.abspath(__file__))
_other_paths = [path for path in sys.path if os.path.abspath(path) != _own_directory]
_previous = importlib.machinery.PathFinder.find_spec("sitecustomize", _other_paths)
if _previous is not None and _previous.loader is not None:
    _module = importlib.util.module_from_spec(_previous)
    sys.modules["sitecustomize"] = _module
    _previous.loader.exec_module(_module)
