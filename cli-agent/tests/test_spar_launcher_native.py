"""The panel must survive a dead container runtime.

A colima content-store fault took Docker down and spar went with it — not because
the panel needs a container, but because the launcher demanded one and the prepare
guard checked a path that only exists inside one. Every harness runs on the host.
"""
import os
import subprocess
from pathlib import Path

import pytest
import yaml as pyyaml

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "workflows/spar/scripts/spar-run.sh"
WORKFLOW = ROOT / "workflows/spar/workflow.yaml"


def _prepare(tmp_path, question="q"):
    """Run the prepare node's shell body in tmp_path, as medulla would."""
    body = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["prepare"]["shell"]
    res = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                         cwd=tmp_path, env={**os.environ, "QUESTION": question},
                         check=False)
    return res.returncode, res.stdout + res.stderr


def test_prepare_accepts_a_non_empty_tree_outside_a_container(tmp_path):
    """The guard used to check /workspace, which exists only inside the container.
    Natively it failed on its own check and the run died before any panelist."""
    (tmp_path / "somefile.py").write_text("x = 1\n")
    rc, out = _prepare(tmp_path)
    assert rc == 0, out
    assert "ready" in out


def test_prepare_still_refuses_an_empty_tree(tmp_path):
    """The guard exists because a failed --mount once produced a verdict from five
    agents reasoning about nothing. Re-pointing it must not disarm it."""
    rc, out = _prepare(tmp_path)
    assert rc != 0
    assert "empty" in out


def test_prepare_ignores_bookkeeping_directories(tmp_path):
    """`.medulla`, `.git` and `box` are ours, not the tree under review."""
    for d in (".medulla", ".git", "box"):
        (tmp_path / d).mkdir()
    rc, out = _prepare(tmp_path)
    assert rc != 0, "a tree holding only our own directories is still empty"


def test_prepare_requires_a_question(tmp_path):
    (tmp_path / "somefile.py").write_text("x = 1\n")
    body = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["prepare"]["shell"]
    env = {k: v for k, v in os.environ.items() if k != "QUESTION"}
    res = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                         cwd=tmp_path, env=env, check=False)
    assert res.returncode != 0
    assert "question" in (res.stdout + res.stderr)


def test_launcher_does_not_hard_require_docker():
    """`command -v docker || die` made a container-runtime outage fatal to a panel
    that does not need a container."""
    src = LAUNCHER.read_text()
    assert 'die "docker is not installed' not in src
    assert 'die "the docker daemon is not responding' not in src
    assert "DOCKER_OK" in src, "the launcher must decide a mode, not assume one"


def test_launcher_still_prefers_docker_when_healthy():
    src = LAUNCHER.read_text()
    assert 'if [ "$DOCKER_OK" = yes ]; then' in src
    assert "--docker --cwd-ro" in src, "the container path keeps the read-only tree"


def test_launcher_announces_the_reduced_native_panel():
    """A thinner panel is a fact the caller must be told, not discover."""
    src = LAUNCHER.read_text()
    assert "3 of 5" in src
    assert "NOT mounted read-only" in src


def test_launcher_liveness_check_works_without_docker():
    """The wait watchdog counted containers; natively that reports every healthy
    panel as dead sixty seconds in."""
    src = LAUNCHER.read_text()
    assert "alive_count()" in src
    assert "docker ps -q --filter 'name=^medulla-'" not in src.split("alive_count() {")[0]
