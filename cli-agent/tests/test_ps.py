"""`medulla ps`: what is running, from docker's index — never from files on disk.

The owner's question was "how many lanes are active and on what task". Before this,
the answer took `docker inspect` per container and a read of its mount list.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from medulla import ps as ps_mod  # noqa: E402


def _row(cid, workflow="spar", lane="2026-09-10_10-12-34-wt-ticket-9d3b",
         workspace="/w/wt-ticket-9d3b", status="Up 14 minutes", labels=None):
    lab = labels if labels is not None else (
        f"medulla.workflow={workflow},medulla.run_dir_name={lane},"
        f"medulla.workspace={workspace}")
    return {"ID": cid, "Names": f"medulla-{lane}", "Status": status, "Labels": lab}


@pytest.fixture
def docker(monkeypatch):
    """Fake `docker ps`, keyed by the filter the command asks with."""
    state = {"labelled": [], "all": []}

    def fake(*filters):
        return state["labelled"] if any(f.startswith("label=") for f in filters) else state["all"]

    monkeypatch.setattr(ps_mod, "_docker_ps", fake)
    return state


def test_it_names_the_workflow_and_the_lane(docker, capsys):
    docker["labelled"] = [_row("abc123456789")]
    docker["all"] = docker["labelled"]
    assert ps_mod.ps([]) == 0
    out = capsys.readouterr().out
    assert "spar" in out and "wt-ticket-9d3b" in out and "1 running" in out


def test_a_workflow_filter_answers_for_that_workflow_only(docker, capsys):
    docker["labelled"] = [_row("a1", workflow="spar"), _row("b2", workflow="mprobe")]
    docker["all"] = docker["labelled"]
    assert ps_mod.ps(["-w", "spar"]) == 0
    out = capsys.readouterr().out
    assert "1 running" in out and "mprobe" not in out


def test_unlabelled_containers_are_not_silently_dropped(docker, capsys):
    """A container from before labels existed is still a running medulla. Listing
    only the labelled ones answers "how many are running" with a number that is
    quietly too small — which is the exact error this command exists to end. Measured
    on the machine the hour this was written: three labelled, five without."""
    docker["labelled"] = []
    docker["all"] = [_row("old1", labels=""), _row("old2", labels="")]
    assert ps_mod.ps([]) == 0
    cap = capsys.readouterr()
    assert "2 more medulla container" in cap.err, "silently reported an empty machine"


def test_it_does_not_read_run_directories(docker, capsys, monkeypatch, tmp_path):
    """A run directory without outcome.json is NOT a running run: measured at a moment
    with three containers up and TEN such directories — seven abandoned or killed.
    Anything that counted them would report activity at three times life, so liveness
    comes from docker and from nowhere else."""
    def explode(*a, **k):
        raise AssertionError("ps touched the filesystem for liveness")
    monkeypatch.setattr(ps_mod.json, "load", explode)
    docker["labelled"] = [_row("a1")]
    docker["all"] = docker["labelled"]
    assert ps_mod.ps([]) == 0


def test_json_output_is_machine_readable(docker, capsys):
    docker["labelled"] = [_row("a1")]
    docker["all"] = docker["labelled"]
    assert ps_mod.ps(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["running"][0]["workflow"] == "spar"


def test_an_empty_machine_says_so(docker, capsys):
    assert ps_mod.ps([]) == 0
    assert "no running" in capsys.readouterr().out


def test_a_broken_docker_row_does_not_lose_the_others(monkeypatch, capsys):
    """One unparseable line must not cost the whole listing."""
    class R:
        returncode = 0
        stdout = '{"ID":"a1","Labels":"medulla.workflow=spar"}\nnot json\n'
        stderr = ""
    monkeypatch.setattr(ps_mod.subprocess, "run", lambda *a, **k: R())
    assert ps_mod.ps([]) == 0
    assert "spar" in capsys.readouterr().out


def test_docker_missing_is_reported_not_crashed(monkeypatch, capsys):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(ps_mod.subprocess, "run", boom)
    assert ps_mod.ps([]) == 1
    assert "docker" in capsys.readouterr().err


def test_one_failing_query_does_not_lose_the_other(monkeypatch, capsys):
    """Seen live while this was written: `docker ps --filter label=...` returned a
    transient snapshotter error from the daemon and the very next query answered
    normally. Under one try/except the blip cost the whole listing, including the half
    already in hand — and the caller saw an error where it could have seen its lanes."""
    def half_broken(*filters):
        if any(f.startswith("label=") for f in filters):
            raise RuntimeError("snapshotter.Usage failed")
        return [_row("a1", labels="")]
    monkeypatch.setattr(ps_mod, "_docker_ps", half_broken)
    assert ps_mod.ps([]) == 0
    err = capsys.readouterr().err
    assert "1 more medulla container" in err, "the containers it DID see went unmentioned"
    assert "may be short" in err, "a partial list must not read as a complete one"


def test_both_queries_failing_is_an_error(monkeypatch, capsys):
    def broken(*a):
        raise RuntimeError("docker daemon is not running")
    monkeypatch.setattr(ps_mod, "_docker_ps", broken)
    assert ps_mod.ps([]) == 1
    assert "daemon" in capsys.readouterr().err
