"""Answering --print-run-dir before the container exists.

Split from docker.py under the project's 250-line rule ($MAX_LOC).
"""
from __future__ import annotations

import datetime
import json as _json
import uuid
from pathlib import Path

from dockerlib.paths import runs_under_for


def announce(args: list[str], workflow, runs_folder) -> tuple[list[str], str | None]:
    """Print where the run will be, and return (args to pass on, the chosen name)."""
    # --print-run-dir, answered NOW. The engine used to print it, which meant waiting
    # out the container bootstrap and its medulla upgrade — ~20s during which an
    # orchestrator has nothing to attach to, and every caller grew the same polling
    # loop. The name is decided here instead and handed in, so the path is known before
    # the container exists. It also fixes whose clock names the run: the container's
    # differs from the host's (a run started at 11:08 was named 09:08).
    # A RESUMED run already has a directory, and it is not ours to name: the engine
    # finds it from the journal. Printing a freshly invented path here would answer the
    # caller with somewhere that will never exist, and the engine would print the real
    # one seconds later — two lines, the wrong one first. So when resuming, step aside
    # and let the engine print, exactly as before.
    resuming = "--resume" in args or "--run" in args
    run_dir_name = None
    if not resuming and ("--print-run-dir" in args or "--print-run-json" in args):
        import datetime
        import uuid
        run_dir_name = (datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        + "-" + uuid.uuid4().hex[:8])
        base = runs_folder or (runs_under_for(Path(workflow)) / "runs" if workflow
                               else Path("runs"))
        host_run_dir = Path(base) / run_dir_name
        if "--print-run-json" in args:
            import json as _json
            args = [a for a in args if a != "--print-run-json"]
            print(_json.dumps(
                {"run_dir": str(host_run_dir), "runs_folder": str(base), "image": image,
                 "started_at": datetime.datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False), flush=True)
        if "--print-run-dir" in args:
            args = [a for a in args if a != "--print-run-dir"]
            print(host_run_dir, flush=True)
    return args, run_dir_name
