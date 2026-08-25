"""A conversation that outlives the run which opened it.

The develop shape: a unit run opens a conversation, a host node starts the landing run,
and that run must reach the SAME agent — a new run directory, an empty store, and the
container kept alive for exactly this.
"""
import json
from pathlib import Path

from conftest import fake_script, read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow
from medulla.v2.harness import resolve as resolve_harness
from medulla.v2.model import AgentSpec

def test_the_conversation_survives_into_the_next_run_of_the_pipeline(tmp_path, monkeypatch):
    """The handoff the whole feature exists for (F6/F10): a develop unit opens a
    conversation, a host node starts the LANDING run, and that run is a new directory
    with an empty store. It kept the container alive and then resumed nothing."""
    from medulla.v2.rundir import RunStore

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("MEDULLA_PIPELINE_ID", "pipe-1")

    first = tmp_path / "run-1"
    first.mkdir()
    RunStore(first, "run-1").record_session("land", "conv-42", "claude-code")

    second = tmp_path / "run-2"          # a different run of the SAME pipeline
    second.mkdir()
    entry = RunStore(second, "run-2").session_entry("land")
    assert entry and entry["id"] == "conv-42"
    assert entry["harness"] == "claude-code"


def test_another_pipeline_sees_nothing(tmp_path, monkeypatch):
    """Two develop units running side by side must not continue each other's agent."""
    from medulla.v2.rundir import RunStore

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("MEDULLA_PIPELINE_ID", "pipe-1")
    first = tmp_path / "a"; first.mkdir()
    RunStore(first, "a").record_session("land", "conv-42", "claude-code")

    monkeypatch.setenv("MEDULLA_PIPELINE_ID", "pipe-2")
    other = tmp_path / "b"; other.mkdir()
    assert RunStore(other, "b").session_entry("land") is None


def test_outside_a_pipeline_nothing_is_shared(tmp_path, monkeypatch):
    """A plain `medulla -w x` keeps its conversations to itself, as before."""
    from medulla.v2.rundir import RunStore

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("MEDULLA_PIPELINE_ID", raising=False)
    first = tmp_path / "a"; first.mkdir()
    RunStore(first, "a").record_session("land", "conv-42", "fake")
    assert not (tmp_path / "home" / ".medulla" / "sessions").exists()

    second = tmp_path / "b"; second.mkdir()
    assert RunStore(second, "b").session_entry("land") is None


def test_the_runs_own_file_still_records_what_it_did(tmp_path, monkeypatch):
    from medulla.v2.rundir import RunStore

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("MEDULLA_PIPELINE_ID", "pipe-1")
    run = tmp_path / "r"; run.mkdir()
    RunStore(run, "r").record_session("land", "conv-7", "codex")
    assert json.loads((run / "sessions.json").read_text())["land"]["id"] == "conv-7"


def test_the_pipeline_file_dies_with_the_pipeline(tmp_path, monkeypatch):
    """The ids name conversations that lived in containers the run just removed.
    Leaving the file would leave a resume that silently opens a NEW conversation
    instead — worse than no file at all."""
    from medulla.v2.rundir import RunStore, drop_pipeline_sessions

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("MEDULLA_PIPELINE_ID", "pipe-9")
    run = tmp_path / "r"; run.mkdir()
    RunStore(run, "pipe-9").record_session("land", "conv-1", "fake")
    shared = tmp_path / "home" / ".medulla" / "sessions" / "pipe-9.json"
    assert shared.exists()

    drop_pipeline_sessions("pipe-9")
    assert not shared.exists()
    assert json.loads((run / "sessions.json").read_text())["land"]["id"] == "conv-1"


def test_a_pipeline_that_never_came_back_is_swept_after_a_day(tmp_path, monkeypatch):
    import os
    import time
    from medulla.v2.rundir import sweep_pipeline_sessions

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    root = tmp_path / "home" / ".medulla" / "sessions"
    root.mkdir(parents=True)
    (root / "old.json").write_text("{}")
    (root / "fresh.json").write_text("{}")
    old_time = time.time() - 30 * 3600
    os.utime(root / "old.json", (old_time, old_time))

    assert sweep_pipeline_sessions() == 1
    assert not (root / "old.json").exists()
    assert (root / "fresh.json").exists()      # a pipeline still running keeps its ids
