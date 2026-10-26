"""Namespace-scoped deployer and rollout tool for curvebender-tools stacks.

Nothing here needs cluster-admin rights: every change is confined to one
namespace, and every object llmd applies or imports is recorded in an
in-cluster inventory so it can be removed exactly.
"""

__version__ = "0.1.0"
