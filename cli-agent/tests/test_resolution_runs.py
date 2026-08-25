"""Where the run directory lands: --runs-folder, MEDULLA_RUNS_UNDER, the definition.

Both paths compute it independently, and a disagreement hands the caller a path that
nothing wrote to.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from conftest import WORKFLOW_BODY, _is_outside_workspace, engine_env, engine_yaml, resolve_or_raise, same_file, write

def test_a_named_runs_folder_beats_every_other_rule(world, tmp_path):
    """The caller NAMED the place, so nothing else gets a say — not the shared-workflow
    classifier, not MEDULLA_RUNS_UNDER. That variable stays sealed (docker.py sets it,
    the engine blanks it for children); the flag is threaded through instead."""
    import os

    from medulla.v2.rundir import runs_root_for

    elsewhere = tmp_path / "outside"
    world.shared("spar")
    world.anchor("spar")
    w = Path(".medulla/workflows/spar")

    assert runs_root_for(w) == Path(".medulla/workflows/spar")      # shared classifier
    assert runs_root_for(w, elsewhere) == elsewhere                 # the flag wins
    os.environ["MEDULLA_RUNS_UNDER"] = "/should/be/ignored"
    try:
        assert runs_root_for(w, elsewhere) == elsewhere             # over the env too
    finally:
        del os.environ["MEDULLA_RUNS_UNDER"]


def test_the_run_and_its_history_land_in_the_named_folder(world, tmp_path):
    from medulla.v2.rundir import RunStore, prune_runs

    elsewhere = tmp_path / "outside"
    world.shared("spar")
    world.anchor("spar")
    store = RunStore.create(Path(".medulla/workflows/spar"), WORKFLOW_BODY,
                            runs_root=elsewhere)

    assert store.dir.is_relative_to(elsewhere)                      # history moved out
    assert not (world.project / ".medulla" / "workflows" / "spar" / "runs").exists()
    prune_runs(Path(".medulla/workflows/spar"), 5, None, elsewhere)  # looks there too


def test_resume_finds_a_run_under_the_named_folder(world, tmp_path):
    """Without this, --resume silently starts fresh instead of continuing."""
    from medulla.v2.engine import find_resumable
    from medulla.v2.rundir import RunStore

    elsewhere = tmp_path / "outside"
    world.shared("spar")
    world.anchor("spar")
    store = RunStore.create(Path(".medulla/workflows/spar"), WORKFLOW_BODY,
                            runs_root=elsewhere)
    w = Path(".medulla/workflows/spar")

    assert find_resumable(w) is None                                 # not where it used to be
    assert find_resumable(w, elsewhere) == store.dir                 # found where told


def test_cwd_ro_mounts_the_workspace_read_only_and_the_runs_folder_writable(dockerpy,
                                                                           world, tmp_path):
    """The pair. The tree is read-only, and the one place the run must write is not —
    mounted at its OWN host path so the dir printed inside is openable outside."""
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()

    plain = dockerpy.build_volumes(tmp_path / "no-claude", mount_agy=False)
    assert f"{world.project}:/workspace" in plain                    # writable by default

    guarded = dockerpy.build_volumes(tmp_path / "no-claude", mount_agy=False,
                                     cwd_ro=True, runs_folder=elsewhere)
    assert f"{world.project}:/workspace:ro" in guarded
    assert f"{elsewhere}:{elsewhere}" in guarded                     # same path, writable


def test_a_named_runs_folder_holds_the_runs_directly(world, tmp_path):
    """Asking for ~/panelbox/p3runs and getting ~/panelbox/p3runs/runs/… is a level
    nobody asked for: the caller named the directory, so the run goes IN it. Without
    the flag the historic <workflow>/runs/ layout is untouched."""
    from medulla.v2.rundir import RunStore, runs_dir_for

    named = tmp_path / "p3runs"
    world.shared("spar")
    world.anchor("spar")
    w = Path(".medulla/workflows/spar")

    assert runs_dir_for(w, named) == named                       # no "runs" appended
    assert runs_dir_for(w) == Path(".medulla/workflows/spar/runs")   # unchanged default

    store = RunStore.create(w, WORKFLOW_BODY, runs_root=named)
    assert store.dir.parent == named                             # runs/<ts>-<id> -> <ts>-<id>


