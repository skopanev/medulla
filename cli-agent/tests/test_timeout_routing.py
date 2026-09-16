"""Timeouts you can route: at a node, and at the whole run.

Two different facts wearing one name. A node that ran out of time has not failed the way
a node that exited 1 has failed — one wants a smaller task, the other wants another
provider — and both used to arrive as __failed__, indistinguishable in a route. A RUN
that ran out of time was worse: it crashed, no node ran, nothing was saved, nobody told.

The compatibility rule underneath both: a workflow that says nothing about timeouts must
behave EXACTLY as it did. Measured before building this — of 66 nodes carrying a timeout
across every workflow on this machine, 66 routed no __timeout__, the pre-push
compatibility gate among them. A fact that broke those would have to be pushed past its
own gate to be introduced.
"""
import json

import pytest
from conftest import read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow


# ── the node fact ───────────────────────────────────────────────────────────

def test_a_node_that_names___timeout___gets_it(tmp_path):
    text = """
version: "2"
start: slow
nodes:
  slow:
    shell: 'sleep 5'
    timeout: 1
    on_signal: {__timeout__: rescue, __failed__: __exit_fail__}
  rescue:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    _run, _outcome, journal = read_run(path.parent)
    assert journal[0]["signal"] == "__timeout__"
    assert journal[0]["next"] == "rescue"


def test_a_node_that_does_not_name_it_sees_exactly_what_it_always_saw(tmp_path):
    """The whole compatibility case in one test: same yaml as before this feature, same
    signal in the journal, same route, same exit code. Not "routes the same" — the
    RECORD must not move either, because every reader of a journal looks for __failed__."""
    text = """
version: "2"
start: slow
nodes:
  slow:
    shell: 'sleep 5'
    timeout: 1
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    _run, outcome, journal = read_run(path.parent)
    assert journal[0]["signal"] == "__failed__"
    assert journal[0]["rc"] == 124 and journal[0]["timed_out"] is True
    assert outcome["error"]["signal"] == "__failed__"


def test_the_full_reason_is_recorded_either_way(tmp_path):
    """Offering the fact rather than imposing it hides nothing: whether or not a node
    routes __timeout__, attempts.jsonl still says which wall was hit."""
    text = """
version: "2"
start: slow
nodes:
  slow:
    agent: {harness: fake, model: "SCRIPT"}
    prompt: p
    timeout: 1
    on_signal: {ok: __exit_ok__}
"""
    from conftest import fake_script
    script = fake_script(tmp_path, "slow.sh", "sleep 5\n")
    path, work = setup(tmp_path, text.replace("SCRIPT", script))
    assert run_workflow(path, workdir=work) == 2
    run, _outcome, _journal = read_run(path.parent)
    rows = [json.loads(l) for l in (run / "attempts.jsonl").read_text().splitlines()]
    assert rows and rows[-1]["reason"] == "timeout"


def test_a_node_with_a_timeout_and_no_route_is_named_in_a_warning(tmp_path, capsys):
    """Compatibility costs silence: an author sets a wall, assumes it is now routable,
    and it is not. The engine says so once per run — a warning, never an error. Making it
    fatal would stop every workflow on this machine at once."""
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    timeout: 30
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0, "a warning must never fail a run"
    err = capsys.readouterr().err
    assert "routes no __timeout__" in err and "'a'" in err


def test_no_warning_once_the_node_routes_it(tmp_path, capsys):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    timeout: 30
    on_signal: {ok: __exit_ok__, __timeout__: __exit_fail__}
"""
    path, work = setup(tmp_path, text)
    run_workflow(path, workdir=work)
    assert "routes no __timeout__" not in capsys.readouterr().err


# ── the run-level handler ───────────────────────────────────────────────────

HANDLER = """
version: "2"
timeout: 2
on_timeout: save_partial
start: grind
nodes:
  grind:
    shell: 'sleep 1; echo "<signal:again>more</signal:again>"'
    timeout: 5
    on_signal: {again: grind}
  save_partial:
    shell: 'echo saved > partial.txt; echo "<signal:ok>k</signal:ok>"'
    timeout: 10
    on_signal: {ok: __exit_fail__}
"""


def test_the_deadline_runs_the_handler_instead_of_crashing(tmp_path):
    path, work = setup(tmp_path, HANDLER)
    rc = run_workflow(path, workdir=work)
    assert rc == 2
    assert (work / "partial.txt").read_text().strip() == "saved"


def test_the_run_still_reports_that_it_timed_out(tmp_path):
    """A handler that finishes ok must not be able to report the run as a success — what
    it managed to save does not change that the run ran out of time."""
    path, work = setup(tmp_path, HANDLER)
    run_workflow(path, workdir=work)
    _run, outcome, _journal = read_run(path.parent)
    assert outcome["outcome"] == "timed_out" and outcome["exit_code"] == 2
    assert outcome["error"]["code"] == "E_DEADLINE"
    assert outcome["error"]["handled_by"] == "save_partial"
    assert outcome["error"]["node"] == "grind", "where the deadline was actually hit"


def test_the_handler_is_not_clamped_to_a_budget_that_is_already_spent(tmp_path):
    """The whole design hinge. The deadline is gone by definition when the handler runs,
    so a handler clamped to what remains is killed in the breath it is started and saves
    nothing. Its own `timeout:` is the only wall it has."""
    text = HANDLER.replace("echo saved > partial.txt", "sleep 1; echo saved > partial.txt")
    path, work = setup(tmp_path, text)
    run_workflow(path, workdir=work)
    assert (work / "partial.txt").exists(), "the handler was killed by the spent deadline"


def test_the_handler_runs_once_and_the_run_ends(tmp_path):
    """A handler that could route back into the graph would be a second run with no
    deadline at all."""
    text = HANDLER.replace("on_signal: {ok: __exit_fail__}", "on_signal: {ok: grind}")
    path, work = setup(tmp_path, text)
    run_workflow(path, workdir=work)
    _run, _outcome, journal = read_run(path.parent)
    assert sum(1 for r in journal if r["node"] == "save_partial") == 1
    assert journal[-1]["node"] == "save_partial"


def test_a_timed_out_run_is_still_resumable(tmp_path):
    """Without on_timeout a deadline is a crash, and `crashed` resumes. Handling it
    gracefully must not quietly cost the run its ability to continue — that would punish
    the workflow that cleaned up after itself."""
    from medulla.v2.engine_run import RESUMABLE_OUTCOMES
    assert "timed_out" in RESUMABLE_OUTCOMES


def test_without_on_timeout_the_deadline_still_crashes(tmp_path):
    """Unchanged for every workflow that does not ask for a handler."""
    text = HANDLER.replace("on_timeout: save_partial\n", "")
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1
    _run, outcome, _journal = read_run(path.parent)
    assert outcome["outcome"] == "crashed" and outcome["error"]["code"] == "E_DEADLINE"


# ── it has to be a real node ────────────────────────────────────────────────

def test_a_typo_in_on_timeout_fails_at_LOAD(tmp_path):
    """Not an hour in, when it is least affordable."""
    from conftest import load_err
    text = HANDLER.replace("on_timeout: save_partial", "on_timeout: save_partal")
    assert "unknown node 'save_partal'" in load_err(tmp_path, text)


def test_on_timeout_without_a_timeout_to_fire_on_is_refused(tmp_path):
    from conftest import load_err
    text = HANDLER.replace("timeout: 2\n", "timeout: 0\n", 1)
    assert "needs a timeout" in load_err(tmp_path, text)
