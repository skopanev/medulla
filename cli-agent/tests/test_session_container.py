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


def test_a_nested_run_keeps_the_pipeline_container(tmp_path, monkeypatch):
    """The develop handoff: `unit` opens a conversation, one of its nodes starts the
    `land` run, and land's agent must reach the SAME container. MEDULLA_RUN_ID is
    reset by every nested medulla, so anchoring to it split them in two."""
    from conftest import read_run, write_workflow
    from medulla.v2.engine import run_workflow
    from dockerlib import keep

    yaml, work = write_workflow(tmp_path, """
version: "2"
start: outer
nodes:
  outer:
    shell: |
      echo "RUN=$MEDULLA_RUN_ID PIPE=$MEDULLA_PIPELINE_ID" > ids.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
""")
    run_workflow(yaml, workdir=work)
    outer = dict(kv.split("=") for kv in (work / "ids.txt").read_text().split())
    assert outer["PIPE"] == outer["RUN"]          # nothing above: the run IS the pipeline

    # now the nested run, started with the pipeline's id in the environment
    monkeypatch.setenv("MEDULLA_PIPELINE_ID", outer["PIPE"])
    yaml2, work2 = write_workflow(tmp_path, """
version: "2"
start: inner
nodes:
  inner:
    shell: |
      echo "RUN=$MEDULLA_RUN_ID PIPE=$MEDULLA_PIPELINE_ID" > ids.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
""", name="inner-wf")
    run_workflow(yaml2, workdir=work2)
    inner = dict(kv.split("=") for kv in (work2 / "ids.txt").read_text().split())

    assert inner["RUN"] != outer["RUN"]           # a different run, as it must be
    assert inner["PIPE"] == outer["PIPE"]         # the same pipeline, which is the point
    assert keep.container_name(inner["PIPE"]) == keep.container_name(outer["PIPE"])


def test_a_nested_run_does_not_reap_the_pipeline(monkeypatch):
    """Cleanup belongs to the top: `land` finishing must not remove a container the
    unit run may still be using."""
    import medulla.v2.engine  # noqa: F401
    from medulla.v2 import engine_run
    calls = []
    monkeypatch.setattr(engine_run.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.delenv("MEDULLA_DOCKER", raising=False)
    monkeypatch.setenv("MEDULLA_PIPELINE_ID", "outer-1")
    engine_run._remove_session_containers("inner-2")
    assert calls == []


def test_the_kept_container_starts_idle_and_waits_for_the_entrypoint(monkeypatch):
    """The race that shipped: the container was started WITH the workflow as its
    command and then `docker exec`-ed the same workflow into it. Two runs, and the
    exec bypassed the entrypoint — so it ran the medulla the image was built with
    while the entrypoint was still upgrading. Live: 4.27.4 answering inside a
    container mid-upgrade to 4.39.1."""
    from dockerlib import session_run

    seen = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    class _Proc:
        returncode = 0
        def wait(self): return 0

    monkeypatch.setattr(session_run.keep, "sweep_stale", lambda *a, **k: 0)
    monkeypatch.setattr(session_run.keep, "is_running", lambda name: False)
    monkeypatch.setattr(session_run.keep, "remove", lambda name: None)
    monkeypatch.setattr(session_run, "_wait_ready", lambda name, **k: seen.append("waited") or True)
    monkeypatch.setattr(session_run.subprocess, "run",
                        lambda cmd, **kw: seen.append(cmd) or _R())
    monkeypatch.setattr(session_run.subprocess, "Popen",
                        lambda cmd, **kw: seen.append(cmd) or _Proc())

    session_run._run_kept("img:1", ["-v", "/a:/a"], ["-w", "wf"], None, None)

    run_cmd = next(c for c in seen if isinstance(c, list) and c[:2] == ["docker", "run"])
    assert run_cmd[-2:] == ["sleep", "infinity"], "the container must start idle"
    assert "--rm" not in run_cmd                     # it has to outlive this run
    assert "-d" in run_cmd

    assert seen.index("waited") < [i for i, c in enumerate(seen)
                                   if isinstance(c, list) and c[:2] == ["docker", "exec"]][0]

    execs = [c for c in seen if isinstance(c, list) and c[:2] == ["docker", "exec"]]
    assert len(execs) == 1, "exactly one exec, or the workflow runs twice"
    assert execs[0][-3:] == ["medulla", "-w", "wf"]


def test_a_container_that_never_ran_is_swept_immediately(monkeypatch):
    """A live session container is always Up — it holds `sleep infinity` while the
    pipeline works. Created or Exited means nothing is waiting on it, so the day-long
    sweep would be keeping rubbish until tomorrow."""
    from dockerlib import keep

    calls = []

    class _R:
        stdout = "dead1 dead2"
    monkeypatch.setattr(keep.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _R())
    assert keep._remove_dead() == 2

    query = calls[0]
    assert "status=created" in query and "status=exited" in query
    assert f"label={keep.LABEL}" in query           # ours only: the daemon is shared
    assert sum(1 for c in calls if c[:3] == ["docker", "rm", "-f"]) == 2
