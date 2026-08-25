"""End to end: deadlines and the walls a run can hit.

Every limit here ends a run in a way the caller must be able to tell apart from a
workflow that simply decided to fail.
"""
import json

import pytest
from medulla.v2.engine import run_workflow

from conftest import read_run, setup_workflow

def test_deadline_exhaustion_is_e_deadline(tmp_path):
    text = """
version: "2"
start: a
timeout: 1
nodes:
  a:
    shell: "sleep 3"
    on_signal: {__failed__: b}
  b:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1       # b never runs: budget is gone
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_DEADLINE"


def test_fast_child_under_small_remaining_budget(tmp_path):
    # clamp, not crash: a consumes most of a 2s budget, b still gets its chance
    text = """
version: "2"
start: a
timeout: 2
nodes:
  a:
    shell: |
      sleep 0.8
      echo "<signal:go>k</signal:go>"
    on_signal: {go: b}
  b:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0


def test_env_exposes_timeout_and_last_event_json(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:go>payload</signal:go>"'
    on_signal: {go: b}
  b:
    shell: |
      echo "t=$MEDULLA_TIMEOUT_S j=$MEDULLA_LAST_EVENT_JSON" > env.txt
      echo "<signal:ok>k</signal:ok>"
    timeout: 42
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    env_txt = (work / "env.txt").read_text()
    assert "t=42" in env_txt and '"node"' in env_txt and "payload" in env_txt


def test_shell_retry_exhaustion_routes_failed(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "exit 1"
    max_attempts: 2
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    _, outcome, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 2
    assert outcome["error"]["signal"] == "__failed__"


def test_defaults_on_signal_tier_routing(tmp_path):
    text = """
version: "2"
start: a
defaults:
  on_signal: {go: b}
nodes:
  a:
    shell: 'echo "<signal:go>k</signal:go>"'
    on_signal: {}
  b:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__, go: __exit_fail__}   # override: defaults self-edge is illegal
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0


def test_missing_harness_binary_is_e_harness(tmp_path, monkeypatch):
    # part 5 wired the real adapters: the razor now fires on a missing binary
    from medulla.v2 import harness as H
    H.reset_registry()
    monkeypatch.setattr("shutil.which", lambda name: None)   # adapters import their own
    text = """
version: "2"
start: a
nodes:
  a:
    agent: codex
    prompt: "p"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_HARNESS"
    H.reset_registry()


def test_run_id_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDULLA_RUN_ID", "corr1234")
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    runs = list((path.parent / "runs").iterdir())
    assert runs[0].name.endswith("-corr1234")


def test_validation_error_before_run_dir(tmp_path):
    text = 'version: "2"\nstart: nope\nnodes:\n  a:\n    shell: "true"\n    on_signal: {ok: __exit_ok__}\n'
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1
    assert not (path.parent / "runs").exists()         # crash before any run dir
