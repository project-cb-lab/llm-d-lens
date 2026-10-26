"""In-place rollouts of a stack with one DisaggregatedSet (``llmd rollouts``).

A rollout moves a running instance from the objects it runs to the ones a source ref
renders. When a DisaggregatedSet's pod templates change, its controller (lws) starts a new
revision and replaces groups in place: with ``maxSurge: 0`` and ``maxUnavailable: N`` per
role, each step removes up to N old groups of every role and creates as many new ones.

llmd plans that roll, applies the change, follows the controller step by step, and runs
the stack's own drain command (``--drain``) for each role while the roll is in progress:
curvebender-tools stacks guard their groups with admission policies that refuse the
controller's scale-down until a group is drained, so the drain is what lets a step proceed.
"""
