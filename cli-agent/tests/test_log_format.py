"""What a run looks like while it is running.

Every line used to be the same width, the same colour and undated, so a long run printed
one undifferentiated sheet: a reader could not see where a step began, which one failed,
or how long the whole thing had been going without doing arithmetic on durations.

The rule these tests exist to hold: colour is decoration and never the only carrier of a
fact. Strip every escape and the line must still say everything it said in colour.
"""
import re

import pytest
from medulla.v2 import logfmt as E

ANSI = re.compile(r"\033\[[0-9;]*m")


@pytest.fixture(autouse=True)
def colour_on(monkeypatch):
    monkeypatch.setenv("MEDULLA_COLOR", "always")


# ── colour is never the only channel ────────────────────────────────────────

def test_the_line_says_everything_without_colour(monkeypatch):
    monkeypatch.setenv("MEDULLA_COLOR", "never")
    plain = E.step_end(9, "measure_diff", "__failed__", "triage", 0.01)
    monkeypatch.setenv("MEDULLA_COLOR", "always")
    painted = ANSI.sub("", E.step_end(9, "measure_diff", "__failed__", "triage", 0.01))
    assert plain == painted
    assert "measure_diff" in plain and "__failed__" in plain and "triage" in plain


def test_a_pipe_gets_no_escapes(monkeypatch):
    # A panel runs in a container with a pipe for stderr; escapes there land in a log
    # file that someone greps later.
    monkeypatch.delenv("MEDULLA_COLOR", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False, raising=False)
    assert ANSI.search(E.step_end(1, "a", "__failed__", "b", 1)) is None


def test_NO_COLOR_wins_over_a_tty(monkeypatch):
    monkeypatch.delenv("MEDULLA_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
    assert not E.colour_enabled()


def test_MEDULLA_COLOR_never_wins_over_NO_COLOR_being_unset(monkeypatch):
    monkeypatch.setenv("MEDULLA_COLOR", "never")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
    assert not E.colour_enabled()


# ── which colour means what ─────────────────────────────────────────────────

def test_failure_is_red_and_success_is_green():
    assert E.signal_colour("__failed__") == "red"
    assert E.signal_colour("__exit_fail__") == "red"
    assert E.signal_colour("__exit_ok__") == "green"
    assert E.signal_colour("__done__") == "green"


def test_default_is_yellow_not_red():
    """__default__ is not a failure — it is a node that concluded without saying
    anything. Painted red, it sends people hunting a crash that never happened."""
    assert E.signal_colour("__default__") == "yellow"


def test_an_authors_own_signal_is_not_graded():
    # `OK` and `retry` are both just where the graph went next; the engine has no
    # opinion about a workflow's vocabulary.
    assert E.signal_colour("OK") == E.signal_colour("retry") == "cyan"


def test_a_terminal_target_carries_its_class():
    # How a run ENDED is what a reader scrolls to find; "-> __exit_fail__" in plain
    # bold looks exactly like "-> next_node".
    assert "\033[31m__exit_fail__" in E.step_end(1, "a", "sig", "__exit_fail__", 1)
    assert "\033[32m__exit_ok__" in E.step_end(1, "a", "sig", "__exit_ok__", 1)
    assert "\033[1mordinary_node" in E.step_end(1, "a", "sig", "ordinary_node", 1)


# ── the clock ───────────────────────────────────────────────────────────────

def test_elapsed_counts_from_the_run_not_from_the_import(monkeypatch):
    """The module is imported long before a run starts — a resumed or long-lived
    process would otherwise open its first step at +14:03."""
    clock = [1000.0]
    monkeypatch.setattr(E.time, "monotonic", lambda: clock[0])
    E.run_started_now()
    clock[0] += 65
    assert E._elapsed() == "+01:05"


def test_hours_appear_only_once_there_are_hours(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(E.time, "monotonic", lambda: clock[0])
    E.run_started_now()
    clock[0] = 192          # 3:12
    assert E._elapsed() == "+03:12"
    clock[0] = 9667         # 2:41:07
    assert E._elapsed() == "+2:41:07"


def test_every_log_line_is_stamped(monkeypatch, capsys):
    E.run_started_now()
    E.log("step 1 | a")
    err = ANSI.sub("", capsys.readouterr().err)
    assert re.match(r"\[medulla\] \d\d:\d\d:\d\d \+\d\d:\d\d  step 1 \| a\n", err), err
