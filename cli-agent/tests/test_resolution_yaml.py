"""One matrix, both paths: does the same -w select the same DEFINITION?

The engine and the container wrapper resolve -w independently — the wrapper picks the
image and the isolation policy, the engine runs the graph. When they disagree the run
is a chimera: the right workflow under another one's image.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from conftest import WORKFLOW_BODY, engine_env, engine_yaml, same_file

# A real, loadable definition: these layouts get validated by load_workflow in the
# resume test, so a shape the contract rejects would fail for the wrong reason.

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


