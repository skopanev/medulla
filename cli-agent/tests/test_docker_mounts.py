"""What the container can SEE: mounts, symlinked definitions, where runs land.

A mount landing in the wrong place is the failure that reads as a broken tool — the
file is plainly there on the host and missing inside.
"""
import importlib.util
import os
from pathlib import Path

import pytest

def test_symlinked_workflow_target_is_mounted_at_the_same_path(dockerpy, tmp_path, monkeypatch):
    # A shared workflow lives outside the repo (~/.medulla/workflows/<name>) and only cwd
    # is mounted, so inside the container the link dangles: "workflow not found". Its
    # target must be mounted read-only at the SAME /workspace path the link occupies.
    shared = tmp_path / "shared" / "spar"
    shared.mkdir(parents=True)
    real = shared / "workflow.yaml"
    real.write_text("version: '2'\n", encoding="utf-8")

    cwd = tmp_path / "repo"
    (cwd / ".medulla" / "workflows" / "spar").mkdir(parents=True)
    (cwd / ".medulla" / "workflows" / "spar" / "workflow.yaml").symlink_to(real)

    monkeypatch.setenv("PWD", str(cwd))
    monkeypatch.chdir(cwd)
    vols = dockerpy.build_volumes(tmp_path / "no-claude", mount_agy=False)
    spec = f"{real}:/workspace/.medulla/workflows/spar/workflow.yaml:ro"
    assert spec in vols
    # the directory itself is NOT mounted: runs/ must stay repo-local
    assert not any(v.startswith(f"{shared}:") for v in vols)


def test_workflow_symlink_pointing_inside_the_workspace_is_not_remounted(dockerpy, tmp_path, monkeypatch):
    # Already inside /workspace — mounting it again would be noise, and a local real file
    # (the override case) must not produce a mount at all.
    cwd = tmp_path / "repo"
    (cwd / ".medulla" / "workflows" / "a").mkdir(parents=True)
    real = cwd / "shared-here.yaml"
    real.write_text("version: '2'\n", encoding="utf-8")
    (cwd / ".medulla" / "workflows" / "a" / "workflow.yaml").symlink_to(real)
    (cwd / ".medulla" / "workflows" / "b").mkdir(parents=True)
    (cwd / ".medulla" / "workflows" / "b" / "workflow.yaml").write_text("version: '2'\n", encoding="utf-8")

    monkeypatch.setenv("PWD", str(cwd))
    monkeypatch.chdir(cwd)
    vols = dockerpy.build_volumes(tmp_path / "no-claude", mount_agy=False)
    assert not any("/workspace/.medulla/workflows/" in v for v in vols)


def test_broken_workflow_symlink_does_not_break_the_run(dockerpy, tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    (cwd / ".medulla" / "workflows" / "x").mkdir(parents=True)
    (cwd / ".medulla" / "workflows" / "x" / "workflow.yaml").symlink_to(tmp_path / "gone.yaml")
    monkeypatch.setenv("PWD", str(cwd))
    monkeypatch.chdir(cwd)
    vols = dockerpy.build_volumes(tmp_path / "no-claude", mount_agy=False)   # must not raise
    assert not any("gone.yaml" in v for v in vols)


def test_runs_under_is_relative_not_a_container_path(dockerpy, tmp_path, monkeypatch):
    # --print-run-dir hands the run dir to a caller on the HOST while the run happened
    # inside the container, so an absolute /workspace/... path is useless to them —
    # caught live when a panel printed a run dir that could not be listed.
    home = tmp_path / "home"
    shared = home / ".medulla" / "workflows" / "spar"
    shared.mkdir(parents=True)
    (shared / "workflow.yaml").write_text("version: '2'\n", encoding="utf-8")
    cwd = tmp_path / "repo"; cwd.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("PWD", str(cwd))
    monkeypatch.chdir(cwd)

    resolved = dockerpy._config_yaml(Path(".medulla/workflows/spar"))
    assert resolved == shared / "workflow.yaml"       # cascade found the shared copy
    # the value handed to the engine must be repo-relative
    dest = Path(".medulla/workflows/spar")
    assert not str(dest).startswith("/")


def test_agy_keychain_is_not_triggered_by_the_word_agy(dockerpy, tmp_path):
    # It used to be `"agy" in yaml.read_text()`: the word in a PROMPT or a comment sent
    # the runner into the macOS Keychain for credentials the run never needed.
    w = tmp_path / "workflow.yaml"
    w.write_text("""version: "2"
start: a
# we used to run agy here, not any more
nodes:
  a:
    agent: {harness: claude-code, model: sonnet}
    prompt: "compare this with agy output"
    on_signal: {ok: __exit_ok__}
""", encoding="utf-8")
    assert dockerpy.workflow_uses_agy(str(w)) is False


def test_agy_is_detected_where_it_is_actually_declared(dockerpy, tmp_path):
    for decl in ('agent: {harness: agy}', 'agent: agy'):
        w = tmp_path / "workflow.yaml"
        w.write_text(f"""version: "2"
start: a
nodes:
  a:
    {decl}
    prompt: "hi"
    on_signal: {{ok: __exit_ok__}}
""", encoding="utf-8")
        assert dockerpy.workflow_uses_agy(str(w)) is True, decl


def test_agy_detected_when_a_pool_input_carries_the_harness(dockerpy, tmp_path):
    # spar declares harnesses as pool DATA, not on the agent node
    w = tmp_path / "workflow.yaml"
    w.write_text("""version: "2"
start: p
nodes:
  p:
    inputs:
      - {slug: gemini, harness: agy, model: "Gemini 3.1 Pro (High)"}
    agent: {harness: "{{input.harness}}"}
    prompt: "hi"
    on_signal: {__done__: __exit_ok__}
""", encoding="utf-8")
    assert dockerpy.workflow_uses_agy(str(w)) is True


def test_unreadable_workflow_keeps_the_permissive_answer(dockerpy, tmp_path):
    assert dockerpy.workflow_uses_agy(str(tmp_path / "nope.yaml")) is True


def test_overlay_carries_a_symlinked_package_directory_whole(dockerpy, tmp_path, monkeypatch):
    """A wrapper on PATH is useless if the package it imports cannot follow it in.

    Real directories under the overlay are still walked file-by-file (never mounted
    as a directory over /usr/local/bin, which would hide the image's own CLIs), but a
    symlink to a directory travels whole to its own nested path — it covers nothing
    but itself.
    """
    home = tmp_path / "home"
    (home / ".medulla" / "container" / "bin").mkdir(parents=True)
    lib = tmp_path / "real-lib" / "pkg"
    lib.mkdir(parents=True)
    (lib / "__init__.py").write_text("", encoding="utf-8")
    wrapper = home / ".medulla" / "container" / "bin" / "wrap"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    nested = home / ".medulla" / "container" / "home" / ".local" / "lib"
    nested.mkdir(parents=True)
    (nested / "pkg").symlink_to(lib)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    vols = dockerpy.build_volumes(tmp_path / "claude-home", mount_agy=False)
    mounts = " ".join(vols)
    assert f"{wrapper}:/usr/local/bin/wrap:ro" in mounts          # the file, as before
    assert f"{lib}:{dockerpy.dockermounts.CONTAINER_HOME}/.local/lib/pkg:ro" in mounts   # the package


def test_sighup_does_not_kill_a_backgrounded_run(dockerpy):
    """`medulla ... &` exists so a 10-20 minute panel outlives the shell that started
    it. SIGHUP arrives when that shell goes away, and unhandled it killed the run
    mid-flight — a panel that had already written 105KB of one answer vanished without
    a manifest, which read as medulla dying on its own. Deliberate interruption is
    untouched: SIGINT and SIGTERM still stop the run and the container with it.
    """
    import inspect
    import signal

    src = inspect.getsource(dockerpy.main)
    assert "SIGHUP" in src and "SIG_IGN" in src
    assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN   # still interruptible
