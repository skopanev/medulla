"""A CLI that started says something within seconds. Silence is a dead process.

Every harness prints an init event early — a session id, a hook response, a step
start. Waiting out a 1800-second body timeout to discover that nothing ever came costs
the whole budget, and then the retry costs it again: a live panel turned a 30-minute
round into an hour that way, three panelists deep.
"""
import time
from pathlib import Path

from medulla.v2 import procrun


def test_a_process_that_says_nothing_is_cut_short(tmp_path, monkeypatch):
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 1)
    started = time.monotonic()
    res = procrun.run("sleep 30", tmp_path, timeout_s=20)
    elapsed = time.monotonic() - started

    assert res.timed_out
    assert elapsed < 10, f"waited {elapsed:.0f}s for a process that never spoke"


def test_a_process_that_speaks_then_thinks_is_left_alone(tmp_path, monkeypatch):
    """The distinction that matters: output early, then a long silence, is an agent
    working — not a dead one."""
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 1)
    res = procrun.run('echo "starting up"; sleep 3; echo "done"', tmp_path, timeout_s=20)
    assert not res.timed_out
    assert "done" in res.stdout


def test_stderr_counts_as_having_spoken(tmp_path, monkeypatch):
    """opencode talks on stderr; a harness is alive whichever pipe it uses."""
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 1)
    res = procrun.run('echo "warming up" >&2; sleep 3', tmp_path, timeout_s=20)
    assert not res.timed_out


def test_a_short_budget_keeps_its_own_timeout(tmp_path, monkeypatch):
    """A hook with a 5-second budget must not be judged by a 60-second silence rule."""
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 60)
    started = time.monotonic()
    res = procrun.run("sleep 10", tmp_path, timeout_s=2)
    assert res.timed_out
    assert time.monotonic() - started < 6      # its own timeout, not the watchdog
