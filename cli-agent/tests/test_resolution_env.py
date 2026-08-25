"""The same matrix, asked what the definition BRINGS with it.

Two live bugs sat here — a workflow .env tier that went missing and a relative
vars.DOCKERFILE resolving against the wrong directory — and neither shows up in a
yaml-path comparison.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from conftest import WORKFLOW_BODY, engine_env, engine_yaml, resolve_or_raise, same_file

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
