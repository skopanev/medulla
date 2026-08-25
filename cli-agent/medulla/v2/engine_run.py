"""Running a workflow end to end: fresh, or resumed.

Split from engine.py under the project's 250-line rule ($MAX_LOC). The Engine executes
a graph; this file is everything around one execution — finding a resumable run, the
signal handlers that make an interrupt clean, and writing the outcome that says what
happened.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .contract import load_workflow
from .engine import Engine
from .engine_scan import log
from .errors import EngineCrash
from .model import TERMINALS
from .rundir import RunLocked, RunStore, launch_dir_of, prune_runs
from .workflow_path import config_yaml


def _normalize_outcome(outcome: dict, store, engine) -> dict:
    """One shape for every outcome.json: steps, duration_s, run_id always
    present — crashed/interrupted used to lack them (found by a field diff
    across real runs). setdefault: paths that computed better values keep them."""
    outcome.setdefault("steps", engine.steps if engine is not None else 0)
    outcome.setdefault("duration_s", round(
        (__import__("datetime").datetime.now() - store.started_at).total_seconds(), 2))
    outcome.setdefault("run_id", store.run_id)
    return outcome


RESUMABLE_OUTCOMES = {"interrupted", "crashed"}   # + no outcome.json at all.
# `crashed` is a documented deviation from the contract's letter: the #1 resume
# trigger is E_DEADLINE, which is a caught crash; config-class crashes just
# crash again identically (same immutable snapshot) — no harm, no data loss.


def find_resumable(workflow_dir: Path, runs_root: Path | None = None) -> Path | None:
    from .rundir import runs_dir_for
    runs_dir = runs_dir_for(workflow_dir, runs_root)
    if not runs_dir.is_dir():
        return None
    for run in sorted((p for p in runs_dir.iterdir() if p.is_dir()),
                      key=lambda p: p.name, reverse=True):   # ts prefix sorts by time
        if not config_yaml(run).is_file():
            continue
        outcome_path = run / "outcome.json"
        if not outcome_path.is_file():
            return run
        try:
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return run                          # torn outcome write = hard-killed mid-finish
        if outcome.get("outcome") in RESUMABLE_OUTCOMES:
            return run
    return None


def run_workflow(
    workflow_path: Path,
    cli_vars: dict[str, str] | None = None,
    start_override: str | None = None,
    workdir: Path | None = None,
    resume_dir: Path | None = None,
    print_run_dir: bool = False,
    runs_root: Path | None = None,
) -> int:
    """Load, run, write outcome.json, return the process exit code (0/1/2/130)."""
    import signal as _signal
    import threading as _threading

    from .procrun import kill_live_processes
    workdir = workdir or Path.cwd()
    store = None
    engine = None

    # SIGTERM (docker stop, systemd) joins the SIGINT path: kill every live
    # child FIRST (pool workers unblock from proc.wait), then raise into the
    # ordinary interrupt flow -> outcome interrupted, exit 130, resumable.
    # v1 had this handler; the rewrite lost it (spar panel, sonnet).
    prev_handlers = {}
    if _threading.current_thread() is _threading.main_thread():
        def _graceful(signum, frame):
            kill_live_processes()
            raise KeyboardInterrupt
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            prev_handlers[sig] = _signal.signal(sig, _graceful)
    try:
        if resume_dir is not None:
            resume_dir = Path(resume_dir)
            outcome_path = resume_dir / "outcome.json"
            if outcome_path.is_file():
                try:
                    prior = json.loads(outcome_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    prior = {}
                if prior.get("outcome") not in RESUMABLE_OUTCOMES:
                    log(f"run {resume_dir.name} already finished "
                        f"(outcome={prior.get('outcome', '?')}); "
                        f"delete outcome.json to force a re-run")
                    return 1
                outcome_path.unlink()           # resuming: the run is live again
            # the SNAPSHOT is the run's config — the live workflow.yaml may have moved on
            workflow = load_workflow(config_yaml(resume_dir))
            workflow.dir = Path(workflow_path).parent if Path(workflow_path).is_file() \
                else Path(workflow_path)
            store = RunStore.open(resume_dir)
            if print_run_dir:
                print(store.dir, flush=True)   # stdout, line 1: caller captures it now
            log(f"resume {store.run_id} -> {store.dir}")
            engine = Engine(workflow, store, workdir,
                            launch_dir=launch_dir_of(store.dir))
            current = engine.replay()
            outcome = (engine.synthesize_terminal(current) if current in TERMINALS
                       else engine.run(start_override=current))
            outcome["duration_s"] = round(
                (__import__("datetime").datetime.now() - store.started_at).total_seconds(), 2)
            store.write_outcome(_normalize_outcome(outcome, store, engine))
            return outcome["exit_code"]

        workflow = load_workflow(Path(workflow_path))
        if cli_vars:
            from .contract import _validate_var_name
            for k in cli_vars:
                _validate_var_name(k, "--var")
            workflow.vars.update({k: str(v) for k, v in cli_vars.items()})
        store = RunStore.create(workflow.dir, workflow.path.read_text(encoding="utf-8"),
                                runs_root=runs_root)
        if print_run_dir:
            print(store.dir, flush=True)   # stdout, line 1 (before the 10-20min engine
                                           # work): a backgrounded caller reads it now,
                                           # relative so it resolves on host under docker
        prune_runs(workflow.dir, workflow.keep_runs, workflow.timeout, runs_root)
        log(f"run {store.run_id} -> {store.dir}")
        engine = Engine(workflow, store, workdir)
        outcome = engine.run(start_override)
        store.write_outcome(_normalize_outcome(outcome, store, engine))
        return outcome["exit_code"]
    except RunLocked as locked:
        log(str(locked))
        return 1
    except EngineCrash as crash:
        outcome = {
            "outcome": "crashed", "exit_code": 1,
            "error": {"code": crash.code, "message": crash.message, "node": crash.node},
        }
        log(f"crash {crash.code}: {crash.message}")
        if store is not None:
            store.write_outcome(_normalize_outcome(outcome, store, engine))
        return 1
    except KeyboardInterrupt:
        if store is not None:
            store.write_outcome(_normalize_outcome(
                {"outcome": "interrupted", "exit_code": 130}, store, engine))
        return 130
    finally:
        for sig, prev in prev_handlers.items():
            try:
                __import__("signal").signal(sig, prev)
            except (ValueError, OSError):
                pass
        if store is not None:
            store.close()                      # release the flock (same-process reruns/tests)
            _remove_session_containers(store.run_id)


def _remove_session_containers(run_id: str) -> None:
    """Remove containers this pipeline kept alive for an agent session.

    A workflow that names a session gets a container that outlives each nested
    `medulla --docker` call — that is how a conversation survives a host step in
    between. Ownership is this run's id, so the run that started them removes them,
    and it happens in `finally`: crashed, interrupted or clean, the container goes.

    Silent when docker is absent or nothing matches — a native run that never used a
    container must not print anything, and a missing daemon is not this run's problem.
    """
    if not run_id or os.environ.get("MEDULLA_DOCKER") == "1":
        return                                 # inside a container: not ours to reap
    try:
        found = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=medulla.session-owner={run_id}"],
            capture_output=True, text=True, timeout=30, check=False).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return
    for cid in found:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, check=False)
    if found:
        log(f"removed {len(found)} session container(s)")
