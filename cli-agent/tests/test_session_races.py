"""The container-reuse races a panel found, all reachable with today's code.

Waiting for readiness only in the create branch, a check-then-create on one unique
name, and an identity that ignored what the container actually IS.
"""
import subprocess
import sys
from pathlib import Path


def test_every_caller_waits_for_readiness_not_just_the_creator(monkeypatch):
    """Three panelists found this independently: waiting only in the create branch
    left whoever arrives SECOND free to exec while the entrypoint was still upgrading
    medulla — the exact race the idle-start rewrite existed to remove."""
    from dockerlib import session_run

    waited, execs = [], []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    class _Proc:
        returncode = 0
        def wait(self): return 0

    monkeypatch.setattr(session_run.keep, "sweep_stale", lambda *a, **k: 0)
    monkeypatch.setattr(session_run.keep, "is_running", lambda name: True)   # already up
    monkeypatch.setattr(session_run.keep, "remove", lambda name: None)
    monkeypatch.setattr(session_run, "_wait_ready", lambda name, **k: waited.append(name) or True)
    monkeypatch.setattr(session_run.subprocess, "run", lambda cmd, **kw: _R())
    monkeypatch.setattr(session_run.subprocess, "Popen",
                        lambda cmd, **kw: execs.append(cmd) or _Proc())

    session_run._run_kept("img:1", ["-v", "/a:/a"], ["-w", "wf"], None, None)
    assert waited, "a caller joining a running container must wait for the marker too"


def test_losing_the_creation_race_joins_instead_of_failing(monkeypatch):
    """Two first callers can both see the container absent. The loser gets "name
    already in use" — which is not an error, it is someone else winning."""
    from dockerlib import session_run

    state = {"running": False}

    class _Fail:
        returncode = 125
        stdout = ""
        stderr = "docker: Error response from daemon: name already in use"

    class _Proc:
        returncode = 0
        def wait(self): return 0

    def is_running(name):
        # absent on the check, present by the time `docker run` has failed
        was = state["running"]
        state["running"] = True
        return was

    monkeypatch.setattr(session_run.keep, "sweep_stale", lambda *a, **k: 0)
    monkeypatch.setattr(session_run.keep, "is_running", is_running)
    monkeypatch.setattr(session_run.keep, "remove", lambda name: None)
    monkeypatch.setattr(session_run, "_wait_ready", lambda name, **k: True)
    monkeypatch.setattr(session_run.subprocess, "run", lambda cmd, **kw: _Fail())
    monkeypatch.setattr(session_run.subprocess, "Popen", lambda cmd, **kw: _Proc())

    assert session_run._run_kept("img:1", [], ["-w", "wf"], None, None) == 0


def test_a_different_image_or_mount_gets_its_own_container():
    """Keying on the pipeline alone let a later nested workflow land in a container
    built from another image, or writable when it asked for --cwd-ro."""
    from dockerlib import keep

    a = keep.spec_digest("img:1", ["-v", "/repo:/workspace:ro"])
    b = keep.spec_digest("img:2", ["-v", "/repo:/workspace:ro"])
    c = keep.spec_digest("img:1", ["-v", "/repo:/workspace"])       # writable
    assert len({a, b, c}) == 3
    assert keep.container_name("pipe1", a) != keep.container_name("pipe1", c)


def test_a_joining_exec_carries_this_calls_environment(monkeypatch, tmp_path):
    """A joining exec inherited the FIRST caller's keys and .env tiers, so a later
    nested run could authenticate as the earlier one — or find nothing at all, if the
    first call had no key and this one does."""
    from dockerlib import env as dockerenv
    from dockerlib import session_run

    envfile = tmp_path / "merged.env"
    envfile.write_text("# a comment\nPROJECT_TIER=from-dotenv\n\n")
    monkeypatch.setattr(dockerenv, "env_file_for_run", str(envfile))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "this-calls-key")

    execs = []

    class _Proc:
        returncode = 0
        def wait(self): return 0

    monkeypatch.setattr(session_run.keep, "sweep_stale", lambda *a, **k: 0)
    monkeypatch.setattr(session_run.keep, "is_running", lambda name: True)
    monkeypatch.setattr(session_run.keep, "remove", lambda name: None)
    monkeypatch.setattr(session_run, "_wait_ready", lambda name, **k: True)
    monkeypatch.setattr(session_run.subprocess, "Popen",
                        lambda cmd, **kw: execs.append(cmd) or _Proc())

    session_run._run_kept("img:1", [], ["-w", "wf"], None, None)

    flat = " ".join(execs[0])
    assert "ANTHROPIC_API_KEY=this-calls-key" in flat
    assert "PROJECT_TIER=from-dotenv" in flat
    assert "# a comment" not in flat            # comments are not variables
