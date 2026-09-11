"""`--graph`: the routing table as a picture, generated from what the engine routes on.

A diagram drawn by hand beside a workflow is a CLAIM about it, and it goes stale on the
next edit — reported from a 16-node graph that went wrong twice in one hour. This is a
VIEW of the same structure `--dry-run` prints, so it cannot disagree with the workflow.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def graph(tmp_path, yaml_text):
    wf = tmp_path / "workflow.yaml"
    wf.write_text(yaml_text)
    res = subprocess.run([sys.executable, "-c",
                          "import sys; sys.path.insert(0, %r); "
                          "from medulla.v2.cli import main; sys.exit(main(sys.argv[1:]))" % str(ROOT),
                          "-w", str(wf), "--graph"],
                         capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return res.stdout


SIMPLE = """
version: "2"
start: a
nodes:
  a:
    shell: echo hi
    on_signal: {ok: b, __failed__: __exit_fail__}
  b:
    shell: echo bye
    on_signal: {ok: __exit_ok__}
"""


def test_every_node_and_every_edge_is_rendered(tmp_path):
    out = graph(tmp_path, SIMPLE)
    assert "flowchart TD" in out
    assert 'a["a<br/>decision · shell"]' in out
    assert "a -->|ok| b" in out
    assert "b -->|ok| __exit_ok__" in out


def test_failure_edges_are_dashed(tmp_path):
    """Solid edges alone are the skeleton. A reader asking "what normally happens"
    should not have to subtract the error wiring by eye."""
    out = graph(tmp_path, SIMPLE)
    assert "a -. __failed__ .-> __exit_fail__" in out
    assert "a -->|__failed__|" not in out


def test_terminals_get_their_own_shape(tmp_path):
    out = graph(tmp_path, SIMPLE)
    assert '__exit_ok__(["__exit_ok__"])' in out
    assert '__exit_fail__(["__exit_fail__"])' in out


def test_the_table_shows_where_edges_CONVERGE(tmp_path):
    """The one thing a diagram renders badly. One workflow had twenty edges arriving
    at a single node, and nobody sees that by following arrows."""
    out = graph(tmp_path, """
version: "2"
start: a
nodes:
  a: {shell: "echo", on_signal: {ok: sink, warn: sink}}
  sink: {shell: "echo", on_signal: {ok: __exit_ok__}}
""")
    table = out.split("| target | in | from |")[1]
    line = next(ln for ln in table.splitlines() if ln.startswith("| sink "))
    assert "| 2 |" in line, line
    assert "a (ok)" in line and "a (warn)" in line
    # busiest target first: the convergence is the finding, not an alphabetical list
    targets = [ln.split("|")[1].strip() for ln in table.splitlines() if ln.startswith("| ")]
    assert targets[0] == "sink", targets


def test_a_pool_is_ONE_box_with_its_policy(tmp_path):
    """Expanding the inputs would draw the roster, not the graph — and the roster
    changes for reasons the graph does not care about."""
    out = graph(tmp_path, """
version: "2"
start: p
nodes:
  p:
    inputs: [{slug: one}, {slug: two}]
    min_success: 1
    agent: {harness: claude-code}
    prompt: "x"
    on_signal: {__done__: __exit_ok__}
""")
    assert "min_success 1" in out
    assert "slug" not in out, "the roster leaked into the picture"


def test_inherited_edges_are_marked(tmp_path):
    """A route that comes from defaults reads identically to one the node declares —
    until you have to change it and cannot find it in the node."""
    out = graph(tmp_path, """
version: "2"
start: a
defaults: {on_signal: {__failed__: __exit_fail__}}
nodes:
  a: {shell: "echo", on_signal: {ok: __exit_ok__}}
""")
    assert "(defaults)" in out


def test_a_dashed_node_name_still_renders(tmp_path):
    """The engine allows [A-Za-z][A-Za-z0-9_-]* in a node name — dashes included —
    and a dash in a mermaid id is not the same thing it is in a name. I first wrote
    this test with a space and learned the engine rejects that outright, so the real
    case is the one it permits."""
    out = graph(tmp_path, """
version: "2"
start: step-one
nodes:
  step-one: {shell: "echo", on_signal: {ok: __exit_ok__}}
""")
    assert "step_one[" in out, "the id must be safe"
    assert "step-one<br/>" in out, "the label must keep the real name"
