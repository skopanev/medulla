"""`medulla launch <workflow> [args...]` — run a workflow's own launcher from anywhere.

A workflow may ship a script that composes its real invocation (spar needs a read-only
mount, a runs folder outside the reviewed tree and a var-file; an agent retyping that
gets it wrong). But a script is a FILE, and the path to it only exists where the
workflow is installed — so `.medulla/workflows/spar/scripts/spar-run.sh` dies with "no
such file or directory" in a git worktree, in a sibling repo, in any tree that does not
carry its own copy. Reported live from a worktree of finik-backend, where the
aggregator's `.medulla` symlink does not follow.

The engine already solved this for itself: `-w spar` resolves by name through the
local-then-machine-wide cascade from any directory. This gives the launcher the same
treatment — same resolver, same rule — so there is one stable command instead of a path
that depends on where you happen to stand.

The launcher runs in the CALLER's directory, not the workflow's: a panel is about the
tree you are in.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .v2.workflow_path import resolve_workflow_yaml


def _scripts(wdir: Path) -> list[Path]:
    d = wdir / "scripts"
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and os.access(p, os.X_OK))


def launch(argv: list[str]) -> int:
    if not argv:
        print("usage: medulla launch <workflow> [args...]", file=sys.stderr)
        print("  runs the workflow's own launcher, found the same way -w finds the", file=sys.stderr)
        print("  workflow: ./.medulla/workflows/<name> first, then ~/.medulla/workflows/<name>",
              file=sys.stderr)
        return 1

    name, rest = argv[0], argv[1:]
    runtime_flags = [flag for flag in ("--docker", "--apple") if flag in rest]
    if len(runtime_flags) > 1:
        print("error: launch --docker and --apple are mutually exclusive", file=sys.stderr)
        return 1
    runtime = "apple" if runtime_flags == ["--apple"] else "docker"
    rest = [arg for arg in rest if arg not in ("--docker", "--apple")]
    # Resolve the DEFINITION, not the directory: an unresolvable name comes back as
    # the caller typed it, and taking its .parent turns `nope` into `.` — which is a
    # directory, so the run got as far as "workflow 'nope' ships no launcher in
    # ./scripts/". The yaml existing is what makes a name a workflow.
    yaml = resolve_workflow_yaml(Path(name))
    wdir = yaml.parent
    if not yaml.is_file():
        print(f"error: no workflow '{name}' (medulla help lists the ones that resolve here)",
              file=sys.stderr)
        return 1

    scripts = _scripts(wdir)
    if not scripts:
        print(f"error: workflow '{name}' ships no launcher in {wdir}/scripts/", file=sys.stderr)
        print(f"  run it directly:  medulla -w {name}", file=sys.stderr)
        return 1
    # One script is the answer. Several means the workflow made a choice, so the
    # caller must too — silently picking the alphabetically-first would be a coin
    # flip between "start the panel" and something else entirely.
    if len(scripts) > 1 and (not rest or rest[0] not in [s.name for s in scripts]):
        print(f"error: workflow '{name}' ships several launchers — name one:", file=sys.stderr)
        for s in scripts:
            print(f"  medulla launch {name} {s.name} ...", file=sys.stderr)
        return 1
    if len(scripts) > 1:
        chosen, rest = next(s for s in scripts if s.name == rest[0]), rest[1:]
    else:
        chosen = scripts[0]

    os.environ["MEDULLA_CONTAINER_RUNTIME"] = runtime
    os.execv(str(chosen), [str(chosen), *rest])   # cwd stays the caller's, on purpose
    return 1                                      # unreachable; execv does not return
