"""Pools: where the inputs come from, and what an empty list means.

A list, a shell command, JSON, JSONL, or nothing at all. Zero inputs is a legitimate
answer that routes (__empty__), not a failure.
"""
import json

from conftest import fake_script, read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow


def read_manifest(run, step_name):
    mp = run / "steps" / step_name / "manifest.jsonl"
    return [json.loads(l) for l in mp.read_text().splitlines()] if mp.exists() else []





# ── inputs: source, sniffing, empty, errors ─────────────────────────────────

def test_source_json_array_object_inputs(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: {shell: "printf '[{\\"id\\": \\"T-1\\", \\"title\\": \\"fix a\\"}, {\\"id\\": \\"T-2\\", \\"title\\": \\"fix b\\"}]'"}
    max_parallel: 2
    shell: 'echo "$MEDULLA_INPUT_ID: {{input.title}}" >> out.txt'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    out = sorted((work / "out.txt").read_text().splitlines())
    assert out == ["T-1: fix a", "T-2: fix b"]
    run, _, _ = read_run(path.parent)
    snapshot = json.loads((run / "steps" / "001-p" / "inputs.json").read_text())
    assert snapshot[0]["id"] == "T-1"


def test_source_plain_lines(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: {shell: "printf 'one\\n\\ntwo\\n'"}
    shell: 'echo "{{input}}" >> got.txt'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert (work / "got.txt").read_text().splitlines() == ["one", "two"]


def test_empty_source_routes_empty_with_manifest(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: {shell: "true"}
    shell: "touch never.txt"
    on_signal: {__done__: __exit_fail__, __empty__: after}
  after:
    shell: |
      wc -l < "$MEDULLA_MANIFEST_P" | tr -d ' ' > count.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert not (work / "never.txt").exists()            # bodies never ran
    assert (work / "count.txt").read_text().strip() == "0"   # empty manifest EXISTS


def test_empty_static_list_routes_empty(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: []
    shell: "true"
    on_signal: {__done__: __exit_fail__, __empty__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0


def test_broken_source_is_e_inputs(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: {shell: "echo partial; exit 3"}
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_INPUTS"


