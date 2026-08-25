"""Is the definition inside the workspace or outside it? The mounts depend on it.

A definition outside the workspace is mounted read-only under /mnt, so runs/ must be
steered elsewhere — a wrong answer here is a container that cannot write its history.
"""
import importlib.util
import sys
from pathlib import Path

import pytest
from conftest import WORKFLOW_BODY, _is_outside_workspace, engine_env, engine_yaml, resolve_or_raise, same_file, write


def test_spelling_the_same_local_workflow_two_ways_classifies_it_the_same(world, dockerpy):
    """A local definition is inside /workspace however it is spelled. Classifying the
    relative spelling as 'outside' builds a bind mount whose SOURCE is a relative
    path — which Docker reads as a named volume, handing the container an empty
    directory instead of the workflow."""
    world.local("spar")
    relative = ".medulla/workflows/spar"
    absolute = str(world.project / ".medulla" / "workflows" / "spar")

    assert _is_outside_workspace(dockerpy, relative) is False
    assert _is_outside_workspace(dockerpy, absolute) is False


def test_a_shared_definition_is_outside_the_workspace_however_it_is_spelled(world, dockerpy):
    world.shared("spar")
    world.anchor("spar")
    relative = ".medulla/workflows/spar"
    absolute = str(world.project / ".medulla" / "workflows" / "spar")

    assert _is_outside_workspace(dockerpy, relative) is True
    assert _is_outside_workspace(dockerpy, absolute) is True


# --------------------------------------------------------------------------
# Layouts a red-team panel found after the first pass
# --------------------------------------------------------------------------

def test_a_workflow_nested_below_the_launch_dir_keeps_its_project_env(world, dockerpy):
    """A repo inside a repo: launch from the outer one, run the inner one's workflow.

    The inner .medulla/.env is a DESCENDANT of the launch dir, not an ancestor, so
    walking up from the launch dir alone never reaches it — and rooting the project
    tier only there silently dropped that secret. Both chains are walked: the launch
    dir's ancestors and the definition's own.
    """
    (world.project / ".medulla" / ".env").write_text("OUTER=from-outer\n", encoding="utf-8")
    inner = world.project / "inner"
    (inner / ".medulla" / "workflows" / "foo").mkdir(parents=True)
    (inner / ".medulla" / ".env").write_text("INNER=from-inner\n", encoding="utf-8")
    (inner / ".medulla" / "workflows" / "foo" / "workflow.yaml").write_text(
        WORKFLOW_BODY, encoding="utf-8")
    w = "inner/.medulla/workflows/foo"

    for env in (engine_env(w), dockerpy._collect_dotenv(w)):
        assert env.get("OUTER") == "from-outer"
        assert env.get("INNER") == "from-inner"


def test_a_workflow_directory_may_carry_a_dot_in_its_name(world):
    """`my.workflows` is a directory, not a file with a suffix. Deciding that from the
    suffix chopped the real name off the path and refused to resolve it at all."""
    shared = world.shared("my.workflows")

    assert same_file(engine_yaml(".medulla/workflows/my.workflows"),
                     shared / "workflow.yaml")


def test_the_image_tag_names_the_workflow_not_the_file(world, dockerpy):
    """`-w .medulla/workflows/spar/workflow.yaml` tagged the image medulla-workflow.yaml:
    the name came from the raw argument instead of the resolved definition."""
    shared = world.shared("spar")
    df = shared / "Dockerfile.spar"
    df.write_text("FROM scratch\n", encoding="utf-8")
    world.anchor("spar")

    for spelling in (".medulla/workflows/spar",
                     ".medulla/workflows/spar/workflow.yaml",
                     "spar"):
        assert dockerpy.image_tag_for(spelling, df).startswith("medulla-spar:")


def test_runs_land_in_the_same_place_whichever_way_the_workflow_is_named(world, dockerpy):
    """The bare engine roots a shared workflow's history at .medulla/workflows/<name>.
    docker.py handed the raw -w over instead, so `-w spar --docker` wrote into
    spar/runs/ at the repo root — same command, different place, and litter outside
    .medulla/."""
    world.shared("spar")
    world.anchor("spar")
    anchor = Path(".medulla/workflows/spar")

    for spelling in ("spar", ".medulla/workflows/spar",
                     ".medulla/workflows/spar/workflow.yaml"):
        assert dockerpy.runs_under_for(Path(spelling)) == anchor

    from medulla.v2.rundir import runs_root_for
    assert runs_root_for(Path(".medulla/workflows/spar")) == anchor   # the bare engine


# --------------------------------------------------------------------------
# Things that must survive the boundary: resume, and nested invocations
# --------------------------------------------------------------------------

def test_resume_from_another_directory_sees_the_same_secrets(world, tmp_path):
    """A run resumed from elsewhere — a CI runner with a fresh checkout path, or just
    another shell — must not silently recompute a different project .env tier. The
    launch directory is recorded in the run and read back on resume."""
    from medulla.v2.engine import load_dotenv
    from medulla.v2.rundir import RunStore, launch_dir_of

    (world.project / ".medulla" / ".env").write_text("PROJECT_KEY=from-project\n",
                                                     encoding="utf-8")
    shared = world.shared("spar")
    world.anchor("spar")
    store = RunStore.create(Path(".medulla/workflows/spar"), WORKFLOW_BODY)
    fresh = load_dotenv(shared, launch_dir=world.project)
    assert fresh["PROJECT_KEY"] == "from-project"

    run_dir = store.dir.resolve()        # absolute NOW: store.dir is relative to the
    elsewhere = tmp_path / "somewhere-else"   # launch dir and stops meaning anything
    elsewhere.mkdir()                         # once we move
    import os
    os.chdir(elsewhere)                                  # resume from a different cwd
    recorded = launch_dir_of(run_dir)
    assert recorded == world.project.resolve()
    assert load_dotenv(shared, launch_dir=recorded) == fresh


def test_a_body_does_not_inherit_the_run_dir_override(world, tmp_path, monkeypatch):
    """MEDULLA_RUNS_UNDER is an internal compensator, and bodies inherit os.environ
    wholesale — so a `medulla` started from inside a body would root ITS history in
    OUR run directory, and that workflow's keep_runs would evict ours. Seen live: a
    panelist's shell inherited it and 96 unrelated tests failed."""
    from medulla.v2.engine import Engine
    from medulla.v2.rundir import RunStore

    monkeypatch.setenv("MEDULLA_RUNS_UNDER", ".medulla/workflows/spar")
    world.shared("spar")
    world.anchor("spar")
    store = RunStore.create(Path(".medulla/workflows/spar"), WORKFLOW_BODY)

    from medulla.v2.contract import load_workflow
    workflow = load_workflow(store.dir / "workflow.yaml")
    engine = Engine(workflow, store, tmp_path)
    assert engine._base_env()["MEDULLA_RUNS_UNDER"] == ""     # does not travel onward


def test_a_typo_inside_the_canonical_form_still_fails(world):
    """The fallback is keyed on the NAME, so a misspelled name finds no machine-wide
    definition and fails — including from a path pointing at another repo. What
    survives is the intended case: any repo may reference the shared definition by
    its real name, and a local copy that exists always wins over it."""
    world.shared("spar")

    assert not resolve_or_raise(".medulla/workflows/sparr").exists()
    assert not resolve_or_raise("/other/repo/.medulla/workflows/spar-typo/missing.yaml").exists()
    assert same_file(resolve_or_raise("/other/repo/.medulla/workflows/spar/workflow.yaml"),
                     world.shared("spar") / "workflow.yaml")


# --------------------------------------------------------------------------
# --runs-folder / --cwd-ro: a panel reads the tree it reviews, never writes it
# --------------------------------------------------------------------------

