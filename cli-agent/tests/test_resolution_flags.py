"""Flags that change what the container sees: --cwd-ro, --var-file, --mount.

Each one exists because a panel had to read a tree without leaving anything in it, or
because a prompt outgrew what an argv string can carry.
"""
import importlib.util
import sys
from pathlib import Path

import pytest
from conftest import WORKFLOW_BODY, _is_outside_workspace, engine_env, engine_yaml, resolve_or_raise, same_file, write


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


def test_a_var_can_come_from_a_file_whole(world, tmp_path):
    """A prompt that goes through a shell argument is at the mercy of quoting, of
    MAX_ARG_STRLEN, and of a variable that quietly lost its content — the last one sets
    an EMPTY question and the panel answers nothing for ten minutes. --var-file reads
    the file, so the text arrives exactly as written and empty is refused."""
    import medulla.v2.cli as cli_mod

    world.shared("spar")
    world.anchor("spar")
    q = tmp_path / "question.md"
    q.write_text('Первая строка\n\nВторая, с "кавычками" и $переменной\n', encoding="utf-8")

    seen = {}

    def fake_run(*a, **k):
        seen.update(k.get("cli_vars") or {})
        return 0

    old = cli_mod.run_workflow
    cli_mod.run_workflow = fake_run
    try:
        rc = cli_mod.main(["-w", ".medulla/workflows/spar", "--var-file", f"QUESTION={q}"])
    finally:
        cli_mod.run_workflow = old
    assert rc == 0
    assert seen["QUESTION"] == q.read_text(encoding="utf-8")     # byte for byte


def test_an_empty_var_file_is_refused(world, tmp_path):
    import medulla.v2.cli as cli_mod

    world.shared("spar")
    world.anchor("spar")
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")

    with pytest.raises(SystemExit):
        cli_mod.main(["-w", ".medulla/workflows/spar", "--var-file", f"QUESTION={empty}"])


# --------------------------------------------------------------------------
# What the panel found in the change above
# --------------------------------------------------------------------------

def test_a_var_too_big_for_the_environment_fails_by_name(world, tmp_path):
    """Linux caps ONE env string at MAX_ARG_STRLEN, so an oversized var kills execve
    for every body — measured in the container: 130KB passed, 200KB raised 'Argument
    list too long' from `true`. That error names neither the var nor the cause, so the
    engine refuses first and says which one."""
    from medulla.v2.contract import load_workflow
    from medulla.v2.engine import Engine
    from medulla.v2.errors import EngineCrash
    from medulla.v2.rundir import RunStore

    world.shared("spar")
    world.anchor("spar")
    store = RunStore.create(Path(".medulla/workflows/spar"), WORKFLOW_BODY)
    workflow = load_workflow(store.dir / "workflow.yaml")
    engine = Engine(workflow, store, tmp_path)
    engine.vars = {"QUESTION": "x" * 200_000}

    with pytest.raises(EngineCrash) as exc:
        engine._base_env()
    assert "QUESTION" in str(exc.value) and "200000" in str(exc.value)


def test_a_var_file_that_is_not_a_regular_file_is_refused(world, tmp_path):
    """A FIFO blocks read_text() forever with nothing on stdout — the run simply never
    starts and never says why."""
    import os

    import medulla.v2.cli as cli_mod

    world.shared("spar")
    world.anchor("spar")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    with pytest.raises(SystemExit):
        cli_mod.main(["-w", ".medulla/workflows/spar", "--var-file", f"Q={fifo}"])
    with pytest.raises(SystemExit):          # and an empty path is not the CWD
        cli_mod.main(["-w", ".medulla/workflows/spar", "--var-file", "Q="])


def test_the_run_dir_name_cannot_escape_the_runs_directory(world):
    """It arrives through the environment, which every body inherits, so `..` or an
    absolute path would move the run somewhere else entirely."""
    import os

    from medulla.v2.rundir import RunStore

    world.shared("spar")
    world.anchor("spar")
    for bad in ("../escaped", "/tmp/absolute", "a/b"):
        os.environ["MEDULLA_RUN_DIR_NAME"] = bad
        try:
            with pytest.raises(RuntimeError):
                RunStore.create(Path(".medulla/workflows/spar"), WORKFLOW_BODY)
        finally:
            del os.environ["MEDULLA_RUN_DIR_NAME"]


def test_a_json_line_in_plain_output_does_not_disarm_the_signal_filter(tmp_path):
    """Any line starting with `{` used to count as "this is our structured output",
    so an agent that printed a JSON snippet inside a plain-text answer had every
    signal in that answer dropped."""
    from medulla.v2 import harness as H

    text = '\n'.join(['Here is the config I found:',
                      '{"unrelated": "json the agent printed"}',
                      '<signal:done>ok</signal:done>'])
    for cls in (H.OpenCodeAdapter, H.AgyAdapter):
        a = cls.__new__(cls)
        assert "<signal:done>" in a.filter_stdout(text), cls.__name__


def test_two_flags_setting_one_var_is_a_crash(world, tmp_path):
    """Silent last-wins hides a real mistake: two flags disagreeing about the same var
    means somebody expected the other one to win."""
    import medulla.v2.cli as cli_mod

    world.shared("spar")
    world.anchor("spar")
    q = tmp_path / "q.md"
    q.write_text("question\n", encoding="utf-8")
    base = ["-w", ".medulla/workflows/spar"]

    with pytest.raises(SystemExit):      # --var-file then --var
        cli_mod.main([*base, "--var-file", f"Q={q}", "--var", "Q=other"])
    with pytest.raises(SystemExit):      # --var-file twice
        cli_mod.main([*base, "--var-file", f"Q={q}", "--var-file", f"Q={q}"])
    with pytest.raises(SystemExit):      # --var twice
        cli_mod.main([*base, "--var", "Q=a", "--var", "Q=b"])


def test_a_failed_agy_turn_yields_no_signal():
    """A FAILED turn can still carry a response, and a signal mined out of it would
    route the graph as though the turn had succeeded."""
    import json as _json

    from medulla.v2 import harness as H

    a = H.AgyAdapter.__new__(H.AgyAdapter)
    ok = _json.dumps({"event": "result",
                      "result": {"status": "SUCCESS",
                                 "response": "<signal:done>ok</signal:done>"}})
    bad = _json.dumps({"event": "result",
                       "result": {"status": "FAILED",
                                  "response": "<signal:done>ok</signal:done>"}})
    assert "<signal:done>" in a.filter_stdout(ok)
    assert a.filter_stdout(bad) == ""
