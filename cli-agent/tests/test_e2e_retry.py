"""End to end: retries, timeouts, and what counts as an attempt.

A timeout is rc 124 by contract, and silence is not a failure worth retrying.
"""
import json

import pytest
from conftest import read_run, setup_workflow
from medulla.v2.engine import run_workflow


def test_timeout_becomes_rc_124_then_failed(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "sleep 5"
    timeout: 1
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    _, outcome, journal = read_run(path.parent)
    assert journal[0]["rc"] == 124 and journal[0]["timed_out"] is True
    assert outcome["error"]["signal"] == "__failed__"


def test_last_message_flows_to_next_node(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:go>payload-42</signal:go>"'
    on_signal: {go: b}
  b:
    shell: |
      echo "env=$MEDULLA_LAST_MESSAGE tmpl={{last.message}}" > received.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    received = (work / "received.txt").read_text()
    assert "env=payload-42" in received and "tmpl=payload-42" in received


def test_node_start_override(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "exit 7"
    on_signal: {ok: __exit_ok__}
  b:
    shell: 'echo "<signal:ok>skipped a</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work, start_override="b") == 0


def test_var_from_failed_attempt_not_applied(tmp_path):
    # fold law: state signals apply atomically from the CONCLUDING attempt only
    text = """
version: "2"
start: a
nodes:
  a:
    shell: |
      if [ -f marker ]; then
        echo "<signal:ok>k</signal:ok>"
      else
        touch marker
        echo "<signal:var key=POISON>from-failed-attempt</signal:var>"
        exit 1
      fi
    max_attempts: 2
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    assert "POISON" not in (run / "vars.yaml").read_text()


def test_single_input_pool_is_still_a_pool(tmp_path):
    # (part-1 not-implemented stub replaced) blind panel edge: a 1-input pool keeps
    # pool semantics — body signals go to the manifest, the join routes __done__
    text = """
version: "2"
start: a
nodes:
  a:
    inputs: [x]
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    _, outcome, journal = read_run(path.parent)
    assert journal[0]["kind"] == "pool" and journal[0]["inputs_ok"] == 1


def test_rendered_empty_shell_is_e_render(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "{{var:MISSING:-}}"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_RENDER"


def test_body_cannot_spoof_engine_facts(tmp_path):
    # a body printing <signal:__failed__> at rc=0 must NOT route the recovery edge
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:__failed__>spoof</signal:__failed__>"'
    on_signal: {__failed__: recover}
  recover:
    shell: 'echo "<signal:ok>reached</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2       # silence -> __default__, not recover
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["signal"] == "__default__"


