"""A dated promise in a comment is a shelf life nobody checks.

Measured here: a seat was swapped "temporarily" with `REVERT ON 2026-09-13` written in
the line above it; the condition that note waited for was met that day, and the note sat
unread for three more — outliving both reasons it existed, while every round ran a seat
short. A reviewer outside the project found it before anyone inside did.

The engine reads the comment because nothing else will. It must NOT act on it: what the
note asks for is a decision, and a decision is not the engine's to take.
"""
import datetime

import pytest
from conftest import read_run
from conftest import write_workflow as setup
from medulla.v2 import rundir
from medulla.v2.engine import run_workflow

WF = """
version: "2"
start: a
nodes:
  a:
    # {note}
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {{ok: __exit_ok__}}
"""


def _run(tmp_path, note, capsys):
    path, work = setup(tmp_path, WF.format(note=note))
    rc = run_workflow(path, workdir=work)
    return rc, capsys.readouterr().err


def test_a_note_that_came_due_is_said_out_loud(tmp_path, capsys):
    rc, err = _run(tmp_path, "REVERT ON 2020-01-01: put the old seat back", capsys)
    assert rc == 0
    assert "came due" in err and "2020-01-01" in err
    assert "put the old seat back" in err, "the note's own words, not a generic warning"


def test_a_note_still_in_the_future_stays_quiet(tmp_path, capsys):
    rc, err = _run(tmp_path, "REVERT ON 2099-12-31: not yet", capsys)
    assert rc == 0 and "came due" not in err


def test_a_note_due_TODAY_is_due(tmp_path, capsys):
    today = datetime.date.today().isoformat()
    _rc, err = _run(tmp_path, f"REVIEW ON {today}: the day itself counts", capsys)
    assert "came due" in err


def test_the_other_verbs_count_too(tmp_path, capsys):
    for verb in ("REVERT", "REVIEW", "REMOVE", "DROP", "EXPIRES"):
        _rc, err = _run(tmp_path, f"{verb} ON 2020-01-01: x", capsys)
        assert "came due" in err, verb


def test_the_engine_does_not_ACT_on_the_note(tmp_path, capsys):
    """It only refuses to let the date pass in silence. Acting would mean the engine
    taking a decision that belongs to whoever wrote the note."""
    rc, _err = _run(tmp_path, "REMOVE ON 2020-01-01: delete the panel node", capsys)
    assert rc == 0, "a due note must never fail or alter a run"


def test_a_malformed_date_is_ignored_not_fatal(tmp_path, capsys):
    rc, err = _run(tmp_path, "REVERT ON soon-ish: no date here", capsys)
    assert rc == 0 and "came due" not in err


def test_past_tense_prose_is_not_a_promise(tmp_path, capsys):
    """"we reverted on 2020-01-01 because it broke" is history, not a promise, and it
    must not warn — a check that cries on every mention of a past date gets muted, and a
    muted check is the one this replaced. The verb must be the bare imperative: `revert`
    followed by `on`, so `reverted on` does not match."""
    _rc, err = _run(tmp_path, "we reverted on 2020-01-01 because it broke", capsys)
    assert "came due" not in err


def test_a_promise_is_found_mid_sentence(tmp_path, capsys):
    """Not every note starts with the verb."""
    _rc, err = _run(tmp_path, "temporary seat swap, REVERT ON 2020-01-01 once quota resets",
                    capsys)
    assert "came due" in err and "once quota resets" in err


def test_the_scan_reads_the_snapshot(tmp_path):
    """Judged as written: the run's own copy, not whatever the file says later."""
    text = "# REVERT ON 2020-01-01: from the snapshot\nversion: '2'\n"
    assert rundir._DATED_NOTE.search(text)
