"""Load-time warnings: things that are legal, parse fine, and are quietly dead.

Split from contract.py under the project's 250-line rule ($MAX_LOC). A warning here
is not a validation failure — every case in this file is a workflow that runs. It
just does not do what its author wrote down, and --validate is where that should
surface rather than six hours into a run.
"""
from __future__ import annotations

import sys


def _warn_dead_budgets(nodes: dict, workflow_timeout: int | None, defaults) -> None:
    """A node budget larger than the run's is silently dead — say so at load time.

    Three independent numbers (workflow, defaults.timeout, a node's own) and nothing
    checking they agree. Found the hard way: a sweep node inherited 3600 while the
    agent workflow it launched declared 21600, so the agent planned for six hours and
    was killed at one. Nothing in the log said the budget had been clamped, because
    from the engine's side nothing went wrong.

    A warning, not a crash: a workflow with timeout 0 (unlimited) is legitimate, and
    so is a generous node budget under a run that will in practice finish sooner. This
    belongs to --validate, which is where you look BEFORE spending six hours.
    """
    if not workflow_timeout:
        return                              # 0/None means unlimited: nothing to exceed
    if defaults and defaults.timeout and defaults.timeout > workflow_timeout:
        print(f"warning: defaults.timeout ({defaults.timeout}s) exceeds the workflow "
              f"timeout ({workflow_timeout}s) — every node is clamped to the smaller one",
              file=sys.stderr)
    for name, node in nodes.items():
        own = getattr(node.action, "timeout", None) if node.action else None
        if own and own > workflow_timeout:
            print(f"warning: node '{name}' asks for {own}s under a workflow timeout of "
                  f"{workflow_timeout}s — it will be clamped, and an agent told it has "
                  f"{own}s will be killed at {workflow_timeout}s", file=sys.stderr)
