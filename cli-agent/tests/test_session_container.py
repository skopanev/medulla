"""A workflow that NAMES a session keeps its container until the pipeline exits.

A conversation lives in the CLI's own state inside $HOME — claude keeps a jsonl file,
opencode a SQLite database, agy a database plus a protobuf. Copying that out is four
different problems; not throwing the container away is one. So: named session → the
container is reused across nested `medulla --docker` calls and removed at the end.
"""
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

WITH_SESSION = """version: "2"
start: a
nodes:
  a:
    agent: {harness: claude-code, session: land}
    prompt: "go"
    on_signal: {done: __exit_ok__}
"""
WITHOUT = """version: "2"
start: a
nodes:
  a:
    agent: {harness: claude-code}
    prompt: "go"
    on_signal: {done: __exit_ok__}
"""


def _workflow(tmp_path, text, name="w"):
    d = tmp_path / name
    d.mkdir()
    (d / "workflow.yaml").write_text(text)
    return str(d)


def test_a_named_session_asks_for_a_kept_container(tmp_path):
    from dockerlib.probe import workflow_names_a_session
    assert workflow_names_a_session(_workflow(tmp_path, WITH_SESSION)) is True


def test_no_session_keeps_todays_behaviour(tmp_path):
    from dockerlib.probe import workflow_names_a_session
    assert workflow_names_a_session(_workflow(tmp_path, WITHOUT, "b")) is False
    assert workflow_names_a_session(None) is False


def test_the_word_session_in_prose_is_not_a_session(tmp_path):
    """It reads the FIELD. A substring match sent the agy probe into the Keychain."""
    from dockerlib.probe import workflow_names_a_session
    prose = WITHOUT.replace('prompt: "go"', 'prompt: "resume the session from before"')
    assert workflow_names_a_session(_workflow(tmp_path, prose, "c")) is False


def test_the_container_name_follows_the_outer_run(monkeypatch):
    """Both nested calls of one pipeline must land on the same name — that is the
    whole mechanism. MEDULLA_RUN_ID is forwarded into every node by the engine."""
    from dockerlib import keep
    monkeypatch.setenv("MEDULLA_RUN_ID", "abc123")
    assert keep.owner_id() == "abc123"
    assert keep.container_name(keep.owner_id()) == "medulla-sess-abc123"
    monkeypatch.delenv("MEDULLA_RUN_ID")
    assert keep.owner_id() == ""          # top of the pipeline: cleans up after itself


def test_stale_containers_are_swept_after_a_day():
    import time

    from dockerlib import keep
    now = time.time()
    fresh = keep._age_seconds(
        time.strftime("%Y-%m-%d %H:%M:%S +0000", time.gmtime(now - 3600)), now)
    old = keep._age_seconds(
        time.strftime("%Y-%m-%d %H:%M:%S +0000", time.gmtime(now - 30 * 3600)), now)
    assert fresh < keep.STALE_AFTER_S < old
    assert keep.STALE_AFTER_S == 24 * 3600
    assert keep._age_seconds("not a timestamp", now) is None


def test_the_engine_removes_its_own_containers_on_exit(monkeypatch):
    """Whatever the outcome — the point of doing it in `finally`."""
    import medulla.v2.engine  # noqa: F401  — imported first: engine_run is half of a cycle
    from medulla.v2 import engine_run

    calls = []

    class _R:
        stdout = "cid1 cid2"
    monkeypatch.setattr(engine_run.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or _R())
    monkeypatch.delenv("MEDULLA_DOCKER", raising=False)
    engine_run._remove_session_containers("run-9")

    assert any("label=medulla.session-owner=run-9" in " ".join(c) for c in calls)
    assert sum(1 for c in calls if c[:3] == ["docker", "rm", "-f"]) == 2


def test_inside_a_container_it_reaps_nothing(monkeypatch):
    """The nested run must not remove the container it is running in."""
    import medulla.v2.engine  # noqa: F401
    from medulla.v2 import engine_run
    calls = []
    monkeypatch.setattr(engine_run.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setenv("MEDULLA_DOCKER", "1")
    engine_run._remove_session_containers("run-9")
    assert calls == []
