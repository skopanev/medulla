"""An AGENT CLI that started says something within seconds. Silence is a dead process.

Every harness prints an init event early — a session id, a hook response, a step
start. Waiting out a 1800-second body timeout to discover that nothing ever came costs
the whole budget, and then the retry costs it again: a live panel turned a 30-minute
round into an hour that way, three panelists deep.

It applies to agent bodies ONLY. A shell body is any program at all, and silence there
means nothing — an Intercom fetcher that runs 140-180 seconds and speaks once it is
done was killed at 60 by the first version of this.
"""
import time
from pathlib import Path

from medulla.v2 import procrun


def test_a_process_that_says_nothing_is_cut_short(tmp_path, monkeypatch):
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 1)
    started = time.monotonic()
    res = procrun.run("sleep 30", tmp_path, timeout_s=20, watch_output=True)
    elapsed = time.monotonic() - started

    assert res.timed_out
    assert elapsed < 10, f"waited {elapsed:.0f}s for a process that never spoke"


def test_a_process_that_speaks_then_thinks_is_left_alone(tmp_path, monkeypatch):
    """The distinction that matters: output early, then a long silence, is an agent
    working — not a dead one."""
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 1)
    res = procrun.run('echo "starting up"; sleep 3; echo "done"', tmp_path,
                       timeout_s=20, watch_output=True)
    assert not res.timed_out
    assert "done" in res.stdout


def test_stderr_counts_as_having_spoken(tmp_path, monkeypatch):
    """opencode talks on stderr; a harness is alive whichever pipe it uses."""
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 1)
    res = procrun.run('echo "warming up" >&2; sleep 3', tmp_path, timeout_s=20,
                       watch_output=True)
    assert not res.timed_out


def test_a_short_budget_keeps_its_own_timeout(tmp_path, monkeypatch):
    """A hook with a 5-second budget must not be judged by a 60-second silence rule."""
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 60)
    started = time.monotonic()
    res = procrun.run("sleep 10", tmp_path, timeout_s=2, watch_output=True)
    assert res.timed_out
    assert time.monotonic() - started < 6      # its own timeout, not the watchdog


def test_a_body_that_stops_talking_is_cut_short_too(tmp_path, monkeypatch):
    """The live failure: three panelists each spoke, then went quiet for 10-14 minutes
    and burned an 1800s timeout — twice, because the retry did the same. A healthy
    round writes an event every few seconds (298 in 586s, longest pause 66s)."""
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 1)
    monkeypatch.setattr(procrun, "IDLE_OUTPUT_S", 2)
    started = time.monotonic()
    res = procrun.run('echo "working"; sleep 30', tmp_path, timeout_s=20, watch_output=True)
    elapsed = time.monotonic() - started

    assert res.timed_out
    assert "working" in res.stdout          # what it managed to say is kept
    assert elapsed < 12, f"waited {elapsed:.0f}s for a body that stopped talking"


def test_steady_output_is_never_cut(tmp_path, monkeypatch):
    """The distinction: talking slowly is working, and must not be killed."""
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 1)
    monkeypatch.setattr(procrun, "IDLE_OUTPUT_S", 2)
    res = procrun.run('for i in 1 2 3 4 5; do echo "step $i"; sleep 1; done',
                      tmp_path, timeout_s=20, watch_output=True)
    assert not res.timed_out
    assert "step 5" in res.stdout


def test_a_shell_body_is_never_watched(tmp_path, monkeypatch):
    """The regression this cost: an Intercom fetcher runs 140-180 seconds and writes
    nothing until it finishes. It was killed at 60 with rc=124 despite a 1800s timeout,
    because the watchdog had been applied to every body rather than to agent CLIs.

    A shell body is any program at all. Silence is not a symptom of anything."""
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 1)
    monkeypatch.setattr(procrun, "IDLE_OUTPUT_S", 1)
    res = procrun.run("sleep 4; echo done", tmp_path, timeout_s=20)   # no watch_output
    assert not res.timed_out
    assert "done" in res.stdout


def test_through_the_engine_a_silent_shell_node_survives(tmp_path, monkeypatch):
    """End to end, the reported shape: a node that works quietly for longer than the
    watchdog would allow, under a generous timeout."""
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 1)
    monkeypatch.setattr(procrun, "IDLE_OUTPUT_S", 1)

    from conftest import read_run, write_workflow
    from medulla.v2.engine import run_workflow

    yaml, work = write_workflow(tmp_path, """
version: "2"
start: fetch
nodes:
  fetch:
    shell: |
      sleep 4
      echo "fetched 1200 conversations"
      echo "<signal:ok>done</signal:ok>"
    timeout: 300
    on_signal: {ok: __exit_ok__}
""")
    run_workflow(yaml, workdir=work)
    _run, out, journal = read_run(yaml.parent)
    assert out["outcome"] == "succeeded"
    assert journal[0]["signal"] == "ok"
