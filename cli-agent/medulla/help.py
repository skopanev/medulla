"""`medulla help` — the launch contract, printed with THIS machine's real paths.

Written for an agent, not for a man page. The failure it exists to stop is a wrong
path: an agent guesses `-w .medulla/workflows/spar` in a repo that has no such
directory, or names a host path in a prompt that the container cannot see, and the
run dies in a way that reads like a broken tool. So this listing is not prose about
where workflows might live — it enumerates the ones that are actually resolvable
right now and prints the exact command for each.

`medulla` with no arguments lands here too. An argparse usage error is the least
useful thing to show someone who does not yet know the shape of the tool.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .v2.workflow_path import config_yaml, shared_workflows


# The runnable definitions on this machine: (name, dir, scope), local first.
# A local copy WINS, so it is listed as the one that answers `-w <name>` and the
# shadowed machine-wide copy is named as shadowed rather than quietly dropped —
# "it ran the other one" is the confusion this whole cascade can cause.
def _discover() -> list[tuple[str, Path, str]]:
    found: list[tuple[str, Path, str]] = []
    seen: set[str] = set()
    for root, scope in ((Path.cwd() / ".medulla" / "workflows", "local"),
                        (shared_workflows(), "machine-wide")):
        if not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            if not config_yaml(d).is_file():
                continue
            scope_now = scope if d.name not in seen else f"{scope}, shadowed by the local copy"
            found.append((d.name, d, scope_now))
            seen.add(d.name)
    return found


def _launcher(d: Path) -> Path | None:
    """A workflow may ship its own launch script — then THAT is the contract.

    spar is the standing example: it needs a read-only mount, a runs folder outside
    the reviewed tree and a var-file, and an agent retyping that from memory gets it
    wrong. Pointing at the script is the difference between "run this" and "compose
    six flags correctly".
    """
    scripts = d / "scripts"
    if not scripts.is_dir():
        return None
    for s in sorted(scripts.glob("*.sh")):
        return s
    return None


def print_help(out=None) -> int:
    w = (out or sys.stdout).write
    w("""medulla — a YAML state machine that runs AI coding agents.
A workflow is a directory with a workflow.yaml; nodes run agents or shell, print
signals, and the signals route the graph.

RUN ONE — the whole launch contract is these two lines:

    medulla -w <name>                  run on the host
    medulla --docker -w <name>         run inside the workflow's image (usual)

  -w takes a BARE NAME, a directory, or a path to a workflow.yaml. Prefer the bare
  name: it resolves from any directory in any repo, so it cannot be a wrong path.
  Resolution order: ./.medulla/workflows/<name> first, then ~/.medulla/workflows/<name>.
""")

    found = _discover()
    if found:
        w("\nAVAILABLE HERE — these resolved just now; copy a line as it stands:\n\n")
        for name, d, scope in found:
            w(f"    medulla --docker -w {name}\n")
            w(f"        {d}  ({scope})\n")
            script = _launcher(d)
            if script is not None:
                w(f"        this one ships a launcher — use it instead: {script}\n")
        w("\n    medulla init <name>            add another (bundled template or scaffold)\n")
    else:
        w("""
AVAILABLE HERE — none. Neither ./.medulla/workflows/ nor ~/.medulla/workflows/
holds a workflow.yaml. Get one:

    medulla init spar              deploy a bundled template machine-wide
    medulla init <name>            scaffold a new workflow in this repo
""")

    w("""
WHERE THE ANSWER LANDS

    runs/<timestamp>-<id>/ beside the definition, unless --runs-folder moves it:
        artifacts/      what the workflow produced — read these
        journal.jsonl   every transition, append-only
        outcome.json    written last; its existence means the run is over
    --print-run-dir prints that directory on stdout at start, before any work.

BEFORE A LONG RUN

    medulla -w <name> --validate       parse and check, run nothing
    medulla -w <name> --dry-run        the above, plus the plan it would execute

PATHS INSIDE --docker — the mistake worth naming twice

    Your working directory is mounted at /workspace and NOTHING else is. A host
    path written into a prompt (~/Projects/thing, ../sibling) does not exist for
    the agents inside; they burn a turn on "No such file or directory".
    Mount it: --mount ../sibling, and it appears at /workspace/sibling. Refer to
    it by THAT path in the prompt.

MORE

    medulla --help          every flag, every MEDULLA_* variable, the signal grammar
    medulla init --help     deploying templates and registering SKILL.md
    medulla upgrade         update this installation
""")
    return 0
