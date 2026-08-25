"""End to end: signals route the graph, and what beats what.

A known signal outranks a non-zero exit; stderr never routes; a dead body is __failed__
and can be routed to recovery like any other outcome.
"""
import json

import pytest
from conftest import read_run, setup_workflow
from medulla.v2.engine import run_workflow


def test_happy_path_vars_and_routing(tmp_path):
    text = """
version: "2"
start: a
vars: {GREETING: hello}
nodes:
  a:
    shell: |
      echo "<signal:var key=TARGET>world</signal:var>"
      echo "<signal:go>ready</signal:go>"
    on_signal: {go: b}
  b:
    shell: |
      echo "computed: {{var:GREETING}} $TARGET"
      [ "$TARGET" = "world" ] && echo "<signal:ok>done</signal:ok>"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, outcome, journal = read_run(path.parent)
    assert outcome["outcome"] == "succeeded" and outcome["steps"] == 2
    assert [r["node"] for r in journal] == ["a", "b"]
    assert journal[0]["signal"] == "go" and journal[1]["next"] == "__exit_ok__"
    vars_yaml = (run / "vars.yaml").read_text()
    assert "TARGET: world" in vars_yaml
    # config snapshot is immutable input for resume
    assert (run / "workflow.yaml").read_text() == text


def test_known_signal_beats_nonzero_exit(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: |
      echo "<signal:go>said it</signal:go>"
      exit 3
    on_signal: {go: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    _, outcome, journal = read_run(path.parent)
    assert journal[0]["rc"] == 3 and journal[0]["signal"] == "go"


def test_stderr_never_routes(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: |
      echo "<signal:go>from stderr</signal:go>" >&2
    on_signal: {go: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2       # silence -> __default__ -> __exit_fail__
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["signal"] == "__default__"


def test_body_death_is_failed_builtin(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "exit 5"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["signal"] == "__failed__"
    assert "rc=5" in outcome["error"]["message"]


def test_failed_can_be_rerouted_to_recovery(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "exit 5"
    on_signal: {__failed__: recover}
  recover:
    shell: 'echo "<signal:ok>saved: $MEDULLA_LAST_MESSAGE</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0


def test_mechanical_retry_within_attempts(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: |
      if [ -f marker ]; then echo "<signal:ok>second try</signal:ok>"; else touch marker; exit 1; fi
    max_attempts: 2
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    _, _, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 2


def test_shell_silence_not_retried(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "true"
    max_attempts: 3
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    _, outcome, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 1                 # deterministic silence: no retry
    assert outcome["error"]["signal"] == "__default__"


