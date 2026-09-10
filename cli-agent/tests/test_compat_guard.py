"""The pre-push gate: break compatibility on purpose, or not at all.

Downstream users find out what changed when their runs stop working. This gate does
not forbid a break — it forbids an UNDECLARED one.
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "hooks"))

import compat_guard as g  # noqa: E402

EMPTY = "0" * 40


def _stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MEDULLA_COMPAT_GUARD", raising=False)


@pytest.fixture
def push(monkeypatch):
    """A push of one ref, with the diff, versions and model verdict all stubbed."""
    state = {"diff": "+a line\n", "old": "4.75.0", "new": "4.76.0",
             "verdict": {"breaking": False, "breaking_changes": [], "project_names": []},
             "raise": None}

    def run_it(text=f"refs/heads/main abc123 refs/heads/main {'d' * 40}"):
        _stdin(monkeypatch, text)
        monkeypatch.setattr(g, "diff_for", lambda *a: state["diff"])
        monkeypatch.setattr(g, "version_at",
                            lambda sha: state["new"] if sha == "abc123" else state["old"])

        def ask(*a):
            if state["raise"]:
                raise RuntimeError(state["raise"])
            return state["verdict"]
        monkeypatch.setattr(g, "ask_engine", ask)
        return g.main([])
    state["run"] = run_it
    return state


# ── what gets read ──────────────────────────────────────────────────────────────

def test_a_branch_deletion_pushes_nothing_to_check():
    assert g.parse_push_refs(f"refs/heads/x {EMPTY} refs/heads/x abc") == []


def test_a_normal_push_is_read_as_local_and_remote():
    assert g.parse_push_refs("refs/heads/m aaa refs/heads/m bbb") == [("aaa", "bbb")]


# ── names ───────────────────────────────────────────────────────────────────────

def test_a_name_added_now_is_caught():
    hits = g.forbidden_names("+# reported by acme-pm during the upgrade\n".replace("acme", "finik"))
    assert hits and "finik" in hits[0].lower()


def test_a_name_being_REMOVED_is_not_a_violation():
    """Otherwise the very commit that cleans the repository cannot be pushed, and
    every commit after it stays red for a word that is no longer there."""
    assert g.forbidden_names("-# reported by finik-pm\n") == []


def test_a_name_the_list_never_heard_of_is_the_models_job(push):
    """The grep is exact about what it knows and blind to the rest. A gate that
    relied on the list alone would pass every project nobody thought to add."""
    push["verdict"] = {"breaking": False, "breaking_changes": [],
                       "project_names": [{"name": "Northwind Bank", "where": "cli.py:12"}]}
    assert push["run"]() == 1


def test_a_major_bump_does_not_excuse_a_name(push):
    """Versioning is the answer to breaking, not to naming somebody else's project."""
    push["old"], push["new"] = "4.75.0", "5.0.0"
    push["diff"] = "+# as reported by finik-pm\n"
    assert push["run"]() == 1


# ── compatibility ───────────────────────────────────────────────────────────────

def test_a_silent_break_stops_the_push(push, capsys):
    push["verdict"] = {"breaking": True, "project_names": [], "breaking_changes": [
        {"what": "--runs-folder renamed to --runs-dir", "who_breaks": "every script",
         "migration": "rename the flag"}]}
    assert push["run"]() == 1
    err = capsys.readouterr().err
    assert "--runs-folder" in err and "every script" in err


def test_the_report_offers_BOTH_ways_forward(push, capsys):
    """A break is a decision the owner makes, not a verdict the gate hands down. A
    gate that only says "no" gets disabled within a week."""
    push["verdict"] = {"breaking": True, "project_names": [],
                       "breaking_changes": [{"what": "x", "who_breaks": "y"}]}
    push["run"]()
    err = capsys.readouterr().err
    assert "MEDULLA_COMPAT_GUARD=off" in err, "no way to ship it knowingly"
    assert "5.0.0" in err, "no way to ship it as a major"


def test_a_declared_major_break_is_allowed_through(push):
    """4.x -> 5.0.0 IS the sanctioned way to break. Stopping it too would leave no
    legitimate path at all."""
    push["old"], push["new"] = "4.75.0", "5.0.0"
    push["verdict"] = {"breaking": True, "project_names": [],
                       "breaking_changes": [{"what": "x", "who_breaks": "y"}]}
    assert push["run"]() == 0


def test_a_minor_bump_is_not_a_declaration(push):
    push["old"], push["new"] = "4.75.0", "4.76.0"
    push["verdict"] = {"breaking": True, "project_names": [],
                       "breaking_changes": [{"what": "x", "who_breaks": "y"}]}
    assert push["run"]() == 1


def test_additions_alone_do_not_stop_anyone(push):
    assert push["run"]() == 0


# ── when the check itself cannot run ────────────────────────────────────────────

def test_a_model_that_cannot_answer_is_not_a_PASS(push, capsys):
    """The failure class this repository has already paid for: a quota check whose
    error branch was `exit 0`, so a check that could not run read as a check that
    passed. An hour of a panel died on it."""
    push["raise"] = "quota exhausted"
    assert push["run"]() == 1
    err = capsys.readouterr().err
    assert "not a pass" in err.lower() and "quota exhausted" in err


def test_the_owner_can_still_push_on_purpose(push, monkeypatch):
    monkeypatch.setenv("MEDULLA_COMPAT_GUARD", "off")
    push["raise"] = "the model is down"
    assert push["run"]() == 0


def test_a_known_name_costs_no_engine_round(push, monkeypatch):
    """The grep is exact and instant. Spending a paid round to be told what a regular
    expression already knows is a minute and a model call for nothing."""
    called = []
    monkeypatch.setattr(g, "ask_engine", lambda *a: called.append(1) or {})
    push["diff"] = "+# reported by finik-pm\n"
    assert push["run"]() == 1
    assert not called, "asked the engine about a name the grep had already found"


def test_the_range_is_what_the_engine_is_asked_about(monkeypatch):
    """The hook knows the two commits; the engine collects the diff itself from them.
    Handing the text across would let the two judge different things."""
    monkeypatch.setattr(g, "run", lambda cmd, **k: type("R", (), {
        "stdout": "deadbeef", "stderr": "", "returncode": 0})())
    assert g.range_for("aaa", "bbb") == "bbb..aaa"
    assert g.range_for("aaa", "0" * 40) == "deadbeef..aaa"


def test_a_version_that_cannot_be_read_is_not_a_major_bump():
    """An unparseable version must not be optimistically treated as a declaration."""
    assert g.major_bumped("", "") is False
    assert g.major_bumped("4.75.0", "not-a-version") is False
    assert g.major_bumped("4.75.0", "5.0.0") is True


# ── how the engine is invoked ───────────────────────────────────────────────────

def test_the_gate_runs_on_the_host_and_from_an_empty_directory(monkeypatch, tmp_path):
    """Two properties in one command line, both easy to undo by accident.

    NOT --docker: the container re-checks and can reinstall the engine on every
    start, and this runs on every push — measured, 17s of review inside a 71s run
    against 33s total on the host.

    cwd is the scratch directory, NOT the repository: on the host there is no
    read-only mount to be had, so the only thing standing between the reviewer and
    the tree it judges is that the tree is not open in front of it.
    """
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["cwd"] = cmd, kw.get("cwd")
        return type("R", (), {"stdout": str(tmp_path), "stderr": "", "returncode": 0})()

    monkeypatch.setattr(g, "run", fake_run)
    monkeypatch.setattr(g, "SCRATCH", tmp_path / "scratch")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "verdict.json").write_text(
        '{"breaking": false, "breaking_changes": [], "project_names": []}')
    g.ask_engine("a..b", "4.75.0", "4.76.0")
    assert "--docker" not in seen["cmd"], "a container on every push"
    assert seen["cwd"] == str(tmp_path / "scratch"), "the gate was handed the repository"
    assert "RANGE=a..b" in " ".join(seen["cmd"])


def test_a_missing_verdict_is_an_error_not_an_empty_finding(monkeypatch, tmp_path):
    """The run directory exists but nothing was written into it. Reading that as
    "no findings" is the fail-open shape; it has to raise so main() fails closed."""
    monkeypatch.setattr(g, "run", lambda cmd, **kw: type("R", (), {
        "stdout": str(tmp_path), "stderr": "", "returncode": 1})())
    monkeypatch.setattr(g, "SCRATCH", tmp_path / "scratch")
    with pytest.raises(RuntimeError, match="no verdict"):
        g.ask_engine("a..b", "1.0.0", "1.0.1")


def test_the_gate_does_not_block_its_own_definition():
    """The list of forbidden words lives in the gate, and its tests spell them too.
    Without an exemption the commit that INSTALLS the gate cannot be pushed, and the
    only ways out are obfuscating the list or disabling the gate to add the gate."""
    diff = ("+++ b/cli-agent/scripts/hooks/compat_guard.py\n"
            '+FORBIDDEN = [r"finik", r"whitebit"]\n')
    assert g.forbidden_names(diff) == []


def test_the_exemption_does_not_leak_to_its_neighbours():
    """Two exact paths, not a directory: otherwise anyone could park a name in a new
    file next door and have it waved through."""
    diff = ("+++ b/cli-agent/scripts/hooks/other.py\n"
            "+# as reported by finik-pm\n")
    assert g.forbidden_names(diff), "a sibling file inherited the exemption"


def test_a_name_is_attributed_to_the_file_it_was_added_in():
    """Two files in one diff: the exemption must follow the +++ header, not the order
    of the lines."""
    diff = ("+++ b/cli-agent/scripts/hooks/compat_guard.py\n"
            '+FORBIDDEN = [r"finik"]\n'
            "+++ b/cli-agent/medulla/cli.py\n"
            "+# workaround for finik\n")
    hits = g.forbidden_names(diff)
    assert len(hits) == 1 and "workaround" in hits[0]
