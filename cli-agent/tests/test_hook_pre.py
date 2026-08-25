"""The pre hook: a guard that can route a node before its body runs.

A pre that emits a known signal skips the body entirely — "skip unless the branch\nmoved" belongs in a hook, not in a node of its own.
"""
import json

from conftest import fake_script, read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow

# ── pre ──────────────────────────────────────────────────────────────────────

def test_pre_envprep_vars_visible_to_body_render(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    pre: 'echo "<signal:var key=NAME>world</signal:var>"'
    shell: |
      [ "{{var:NAME}}" = "world" ] && echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0


def test_pre_guard_skips_body(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    pre: 'echo "<signal:done_already>cached</signal:done_already>"'
    shell: "touch should-not-exist"
    on_signal: {done_already: __exit_ok__, ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert not (work / "should-not-exist").exists()
    _, _, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 0 and journal[0]["signal"] == "done_already"


def test_pre_failure_is_failed(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    pre: "exit 7"
    shell: "touch should-not-exist"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    assert not (work / "should-not-exist").exists()
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["signal"] == "__failed__"
    assert "pre hook failed" in outcome["error"]["message"]


def test_pre_vars_applied_even_when_guard_routes(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    pre: |
      echo "<signal:var key=CACHE>hit</signal:var>"
      echo "<signal:skip>guarded</signal:skip>"
    shell: "true"
    on_signal: {skip: __exit_ok__, ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    assert "CACHE: hit" in (run / "vars.yaml").read_text()


