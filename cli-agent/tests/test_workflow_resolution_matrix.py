"""One matrix, both execution paths: does the same `-w` mean the same thing?

The engine (v2/cli.py) and the container wrapper (scripts/docker.py) each resolve
`-w` independently. The wrapper picks the image and the tmpfs isolation policy; the
engine runs the graph. When they disagree the run is a chimera — the right workflow
executed under another one's image, or with an isolation policy it never declared.

A string-equality "parity" check is not enough: the two live bugs this file was
written for (the workflow `.env` tier and a relative `vars.DOCKERFILE`) are in
functions that return an env dict and a path, not a yaml path. So every layout below
is asked what actually CAME OUT, not whether two resolvers agree:

  * which yaml executes                      -> the graph
  * which .env keys reach the body           -> the secrets
  * which Dockerfile builds the image        -> the runtime
  * is the definition classified in/out      -> the mounts

Layouts are the ones that bite: shared-only (the README's flagship, no local file at
all), zero-byte debris, the pre-4.1 pipeline.yaml, a yaml named directly, and a path
that simply does not exist.
"""
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture
def dockerpy():
    spec = importlib.util.spec_from_file_location(
        "dockerpy", Path(__file__).resolve().parent.parent / "scripts" / "docker.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# A real, loadable definition: these layouts get validated by load_workflow in the
# resume test, so a shape the contract rejects would fail for the wrong reason.
WORKFLOW_BODY = ("version: '2'\nstart: a\nnodes:\n  a:\n"
                 "    shell: 'true'\n    on_signal: {ok: __exit_ok__}\n")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A machine: a HOME with a shared workflow root, and a project to launch from.

    Both resolvers read Path.home(); the project is the cwd, because that is what
    --docker mounts as /workspace and what a relative -w is spelled against.
    """
    home = tmp_path / "home"
    (home / ".medulla" / "workflows").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    project = tmp_path / "proj"
    (project / ".medulla" / "workflows").mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("PWD", str(project))   # docker.py reads $PWD, not just getcwd()

    class World:
        def __init__(self):
            self.home = home
            self.project = project

        def shared(self, name, *, yaml_name="workflow.yaml", body=WORKFLOW_BODY, env=None):
            d = home / ".medulla" / "workflows" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / yaml_name).write_text(body, encoding="utf-8")
            if env:
                (d / ".env").write_text(env, encoding="utf-8")
            return d

        def local(self, name, *, yaml_name="workflow.yaml", body=WORKFLOW_BODY, env=None):
            d = project / ".medulla" / "workflows" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / yaml_name).write_text(body, encoding="utf-8")
            if env:
                (d / ".env").write_text(env, encoding="utf-8")
            return d

        def anchor(self, name):
            """The directory 12 repos actually have: no yaml, just a home for runs/."""
            d = project / ".medulla" / "workflows" / name
            d.mkdir(parents=True, exist_ok=True)
            return d

    return World()


def engine_yaml(w):
    from medulla.v2.cli import _resolve_workflow_yaml
    return _resolve_workflow_yaml(Path(w))


def resolve_or_raise(w):
    return engine_yaml(w)


def same_file(a, b):
    """Identity of the FILE, not the spelling of the path: both resolvers hand back
    the path as it was given (deliberately — an absolutised one breaks
    --print-run-dir across the container boundary), so a relative and an absolute
    spelling of one file are the same answer here."""
    return Path(a).resolve() == Path(b).resolve()


def engine_env(w):
    from medulla.v2.engine import load_dotenv
    return load_dotenv(engine_yaml(w).parent)


# --------------------------------------------------------------------------
# Which yaml executes
# --------------------------------------------------------------------------

def test_shared_only_is_the_same_yaml_in_both_paths(world, dockerpy):
    """The README's flagship: `cd any-repo && medulla -w .medulla/workflows/spar`,
    no local copy, no symlink, no flag."""
    shared = world.shared("spar")
    world.anchor("spar")
    w = ".medulla/workflows/spar"

    assert same_file(engine_yaml(w), shared / "workflow.yaml")
    assert same_file(dockerpy._config_yaml(Path(w)), shared / "workflow.yaml")


def test_a_local_copy_beats_the_shared_one_in_both_paths(world, dockerpy):
    world.shared("spar")
    local = world.local("spar")
    w = ".medulla/workflows/spar"

    assert same_file(engine_yaml(w), local / "workflow.yaml")
    assert same_file(dockerpy._config_yaml(Path(w)), local / "workflow.yaml")


def test_zero_byte_debris_loses_to_shared_in_both_paths(world, dockerpy):
    """fback-yimerxmy0y: an empty workflow.yaml appeared in a repo (a bind-mount
    target the daemon created) and outranked the machine-wide definition."""
    shared = world.shared("spar")
    local = world.local("spar", body="")
    assert (local / "workflow.yaml").stat().st_size == 0
    w = ".medulla/workflows/spar"

    assert same_file(engine_yaml(w), shared / "workflow.yaml")
    assert same_file(dockerpy._config_yaml(Path(w)), shared / "workflow.yaml")


def test_zero_byte_debris_does_not_hide_a_real_legacy_pipeline(world, dockerpy):
    """Debris beside a REAL pre-4.1 pipeline.yaml (10+ of those still live on this
    machine). An empty file carries no intent, so it must not shadow the working
    definition next to it either — one rule, uniform: zero bytes means the file is
    not there. The engine used to fall through to the shared copy and abandon the
    local pipeline.yaml; docker.py's guard never inspected pipeline.yaml at all and
    kept the local directory, so the two picked different definitions."""
    world.shared("spar")
    local = world.local("spar", body="")
    (local / "pipeline.yaml").write_text(WORKFLOW_BODY, encoding="utf-8")
    w = ".medulla/workflows/spar"

    assert same_file(engine_yaml(w), dockerpy._config_yaml(Path(w)))
    assert same_file(engine_yaml(w), local / "pipeline.yaml")


def test_a_shared_legacy_pipeline_is_found_by_both_paths(world, dockerpy):
    """Backward compatibility must not depend on whether --docker was passed."""
    shared = world.shared("old", yaml_name="pipeline.yaml")
    world.anchor("old")
    w = ".medulla/workflows/old"

    assert same_file(engine_yaml(w), shared / "pipeline.yaml")
    assert same_file(dockerpy._config_yaml(Path(w)), shared / "pipeline.yaml")


def test_a_yaml_named_directly_is_the_answer_in_both_paths(world, dockerpy):
    """`-w brain/resolve.yaml`: one workflow among several in a directory."""
    d = world.project / "brain"
    d.mkdir()
    (d / "resolve.yaml").write_text(WORKFLOW_BODY, encoding="utf-8")
    w = "brain/resolve.yaml"

    assert same_file(engine_yaml(w), d / "resolve.yaml")
    assert same_file(dockerpy._config_yaml(Path(w)), d / "resolve.yaml")


def test_a_path_that_does_not_exist_fails_instead_of_guessing(world):
    """A missing path whose PARENT happens to be named like a shared workflow used to
    silently run that workflow: `typo/spar/missing.yaml` ran spar. A typo must not
    select a workflow — the canonical shared form is what earns the fallback."""
    world.shared("spar")

    got = engine_yaml("typo/spar/missing.yaml")
    assert not same_file(got, world.shared("spar") / "workflow.yaml")
    assert not got.exists()          # loud "workflow not found: typo/spar/missing.yaml"


def test_the_canonical_shared_form_still_falls_back(world):
    """The fallback that must survive: the workflow dir exists as a bare anchor (12
    repos on this machine look exactly like this) or does not exist at all."""
    shared = world.shared("spar")
    world.anchor("spar")
    assert same_file(engine_yaml(".medulla/workflows/spar"), shared / "workflow.yaml")

    import shutil
    shutil.rmtree(world.project / ".medulla" / "workflows" / "spar")
    assert same_file(engine_yaml(".medulla/workflows/spar"), shared / "workflow.yaml")


# --------------------------------------------------------------------------
# Which .env keys reach the body
# --------------------------------------------------------------------------

def test_the_workflow_env_tier_of_a_shared_definition_reaches_both_paths(world, dockerpy):
    """`<workflow>/.env` is a documented tier (README). For a shared definition it
    lives beside the shared yaml — docker.py derived the directory from the raw -w
    argument, which for the flagship layout is a path that does not exist, so the
    tier was silently empty under --docker only."""
    world.shared("spar", env="SHARED_WORKFLOW_KEY=from-shared\n")
    world.anchor("spar")
    w = ".medulla/workflows/spar"

    assert engine_env(w).get("SHARED_WORKFLOW_KEY") == "from-shared"
    assert dockerpy._collect_dotenv(w).get("SHARED_WORKFLOW_KEY") == "from-shared"


def test_all_three_env_tiers_agree_across_both_paths(world, dockerpy):
    """Same command, same secrets, --docker or not (hard constraint 5). The project
    tier belongs to the repo you LAUNCH from, which for a shared definition is not
    an ancestor of the yaml."""
    (world.home / ".medulla" / ".env").write_text(
        "GLOBAL_KEY=global\nOVERRIDDEN=global\n", encoding="utf-8")
    (world.project / ".medulla" / ".env").write_text(
        "PROJECT_KEY=project\nOVERRIDDEN=project\n", encoding="utf-8")
    world.shared("spar", env="OVERRIDDEN=workflow\n")
    world.anchor("spar")
    w = ".medulla/workflows/spar"

    from_engine, from_docker = engine_env(w), dockerpy._collect_dotenv(w)
    for env in (from_engine, from_docker):
        assert env.get("GLOBAL_KEY") == "global"
        assert env.get("PROJECT_KEY") == "project"
        assert env.get("OVERRIDDEN") == "workflow"        # nearest tier wins
    assert from_engine == from_docker


# --------------------------------------------------------------------------
# Which Dockerfile builds the image
# --------------------------------------------------------------------------

def test_a_relative_dockerfile_in_a_shared_definition_resolves_beside_it(world, dockerpy):
    """A relative vars.DOCKERFILE is relative to the workflow's own directory. For a
    shared definition that is the shared directory — resolving it against the
    (nonexistent) repo-local path fails the build on a path that cannot exist."""
    shared = world.shared(
        "spar", body=WORKFLOW_BODY + "vars:\n  DOCKERFILE: Dockerfile.spar\n")
    (shared / "Dockerfile.spar").write_text("FROM scratch\n", encoding="utf-8")
    world.anchor("spar")

    assert same_file(dockerpy.resolve_dockerfile(".medulla/workflows/spar", {}), shared / "Dockerfile.spar")


def test_a_relative_dockerfile_in_a_local_definition_still_resolves_beside_it(world, dockerpy):
    local = world.local(
        "spar", body=WORKFLOW_BODY + "vars:\n  DOCKERFILE: Dockerfile.spar\n")
    (local / "Dockerfile.spar").write_text("FROM scratch\n", encoding="utf-8")

    assert same_file(dockerpy.resolve_dockerfile(".medulla/workflows/spar", {}), local / "Dockerfile.spar")


# --------------------------------------------------------------------------
# Is the definition inside the workspace (mount classification)
# --------------------------------------------------------------------------

def _is_outside_workspace(dockerpy, w):
    """Ask docker.py itself — a copy of the rule here would only confirm the copy."""
    return dockerpy.definition_is_outside_workspace(dockerpy._config_yaml(Path(w)))


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


def test_cwd_ro_makes_the_mount_points_it_needs(dockerpy, world, tmp_path, monkeypatch):
    """A nested mount needs its point to exist, and the daemon lays /workspace down
    first — read-only under --cwd-ro, so it cannot create /workspace/<name> itself and
    the run dies before starting. That is exactly the case the flag exists for: a panel
    launched from an empty box that brings its repositories in with --mount."""
    world.shared("spar")
    world.anchor("spar")
    repo = tmp_path / "some-repo"
    repo.mkdir()
    point = world.project / "some-repo"
    assert not point.exists()                                    # empty box

    monkeypatch.setattr(dockerpy, "run_docker",
                        lambda *a, **k: 0 if point.is_dir() else 99)
    monkeypatch.setattr(sys, "argv",
                        ["docker.py", "--cwd-ro", "--runs-folder", str(tmp_path / "out"),
                         "--mount", str(repo), "-w", ".medulla/workflows/spar"])
    monkeypatch.setattr(dockerpy, "assert_runs_folder_reaches_the_container",
                        lambda *a, **k: None)
    monkeypatch.setattr(dockerpy, "ensure_image", lambda *a, **k: 0)
    monkeypatch.setattr(dockerpy, "image_home", lambda image, fallback: fallback)

    assert dockerpy.main() == 0                                  # the point existed
    assert not point.exists()                                    # and was taken away again
