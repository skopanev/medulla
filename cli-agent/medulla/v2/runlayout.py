"""Where a run's directory goes, and which old ones may be removed.

Split from rundir.py under the project's 250-line rule ($MAX_LOC). RunStore is about
WRITING a run; this file is about WHERE — a question that has been answered wrongly
several times, and every comment below records one of those times.
"""
from __future__ import annotations

import datetime
import os
import shutil
from pathlib import Path

from .workflow_path import config_yaml


def runs_root_for(workflow_dir: Path, runs_root: Path | None = None) -> Path:
    """Where this workflow's runs/ live.

    runs_root (--runs-folder) wins over everything: the caller NAMED the place, and
    naming it is the whole point — a panel that must not write into the tree it
    reviews sends its history somewhere else entirely. There is no default for it
    and no environment variable to set it from; it arrives as an argument or not at
    all.

    Normally: right beside the workflow, unchanged — that is its home and resume,
    prune and artifacts have always looked there.

    The exception is a SHARED definition (~/.medulla/workflows/<name>): one copy per
    machine serving many repos. Its history must NOT pool there — every project would
    dump runs into one directory, prune would evict other repos' history, and under
    --docker that path is mounted read-only anyway. So a shared workflow writes into
    the directory medulla was LAUNCHED from, which is the project root and exactly
    what --docker mounts as /workspace.
    """
    if runs_root is not None:
        return Path(runs_root)
    # Set by scripts/docker.py when the definition is mounted from OUTSIDE the workspace:
    # the yaml then sits on a read-only path, so history must be told where to go.
    under = os.environ.get("MEDULLA_RUNS_UNDER")
    if under:
        return Path(under)
    resolved = workflow_dir.resolve()          # resolve only to CLASSIFY, never to return:
    shared_root = (Path.home() / ".medulla" / "workflows").resolve()
    if not (shared_root == resolved or shared_root in resolved.parents):
        # Repo-local workflow: hand back the path AS GIVEN. Returning the resolved one
        # made --print-run-dir emit /workspace/... under --docker — a path that does not
        # exist for the caller on the host.
        return workflow_dir
    # RELATIVE on purpose: --print-run-dir hands this path to the caller, who is on the
    # HOST while the run
    # happened inside the container: an absolute /workspace/... path would not exist for
    # them. Relative to the launch dir it is valid in both places, and it is the same
    # location --docker mounts, so --resume works across host and container runs.
    return Path(".medulla") / "workflows" / resolved.name


def launch_dir_of(run_dir: Path) -> Path | None:
    """The directory a run was started in, or None for runs from before this was
    recorded (they fall back to the current one — the old behaviour)."""
    try:
        return Path((run_dir / "launch.txt").read_text(encoding="utf-8").strip())
    except OSError:
        return None


def runs_dir_for(workflow_dir: Path, runs_root: Path | None = None) -> Path:
    """The directory the run directories sit in.

    With --runs-folder the caller named THAT directory, so nothing is appended: asking
    for ~/panelbox/p3runs and getting ~/panelbox/p3runs/runs/... is a level nobody
    asked for. Without it the historic layout stands: <workflow>/runs/.
    """
    if runs_root is not None:
        return Path(runs_root)
    return runs_root_for(workflow_dir) / "runs"


def prune_runs(workflow_dir: Path, keep_runs: int, workflow_timeout: int | None,
               runs_root: Path | None = None) -> None:
    """On boot, after the new run dir exists. Finished (has outcome.json): keep the
    newest keep_runs. Unfinished: never touch while younger than the workflow
    timeout (the active-run shield); timeout 0/None = never auto-prune unfinished."""
    runs_dir = runs_dir_for(workflow_dir, runs_root)
    if not runs_dir.is_dir():
        return
    finished: list[Path] = []
    now = datetime.datetime.now()
    for run in runs_dir.iterdir():
        if not run.is_dir():
            continue
        if (run / "outcome.json").is_file():
            finished.append(run)
            continue
        if workflow_timeout:
            try:
                ts = datetime.datetime.strptime(run.name.rsplit("-", 1)[0], "%Y-%m-%d_%H-%M-%S")
            except ValueError:
                continue                       # unrecognized name: leave it alone
            if (now - ts).total_seconds() > workflow_timeout * 2:
                shutil.rmtree(run, ignore_errors=True)   # certainly dead: deadline long past
    for run in sorted(finished, key=lambda p: p.name, reverse=True)[keep_runs:]:
        shutil.rmtree(run, ignore_errors=True)
