"""`env:` on a node and on a pool input — variables that do not outlive their node.

The only per-node route before this was a pre hook printing <signal:var>, and that
value outlives its node: measured on a live run, an AGENT_ROLE set by the triage node
was still in vars.yaml afterwards. A node whose pre was forgotten then inherits a
neighbour's value SILENTLY — no error, just different behaviour. It is also an
assignment disguised as printing a signal.

The driver: each node must receive its own contract from a memory store by its OWN
role, and the hook reads that role from the environment.
"""
import subprocess
import sys
from pathlib import Path

import pytest
import yaml as pyyaml

ROOT = Path(__file__).resolve().parent.parent


def run(tmp_path, yaml_text):
    (tmp_path / "workflow.yaml").write_text(yaml_text)
    res = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); "
         "from medulla.v2.cli import main; sys.exit(main(sys.argv[1:]))" % str(ROOT),
         "-w", str(tmp_path / "workflow.yaml")],
        capture_output=True, text=True, cwd=tmp_path, check=False)
    runs = sorted((tmp_path / "runs").glob("*/"), key=lambda p: p.stat().st_mtime)
    return res, (runs[-1] if runs else None)


def bodies(run_dir):
    return "\n".join(p.read_text(errors="replace")
                     for p in run_dir.glob("steps/*/**/attempt-*.txt"))


def test_a_node_env_reaches_its_own_body(tmp_path):
    res, run_dir = run(tmp_path, """
version: "2"
start: a
nodes:
  a:
    env: {AGENT_ROLE: triage}
    shell: |
      echo "SAW=$AGENT_ROLE"
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
""")
    assert "SAW=triage" in bodies(run_dir), res.stderr


def test_it_does_NOT_reach_the_next_node(tmp_path):
    """The whole point. With a pre hook this value is still set two nodes later."""
    res, run_dir = run(tmp_path, """
version: "2"
start: a
nodes:
  a:
    env: {AGENT_ROLE: triage}
    shell: "echo '<signal:ok>k</signal:ok>'"
    on_signal: {ok: b}
  b:
    shell: |
      echo "SAW=[$AGENT_ROLE]"
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
""")
    assert "SAW=[]" in bodies(run_dir), bodies(run_dir)


def test_it_is_not_written_into_the_run_vars(tmp_path):
    """vars.yaml is the run's record. A per-node value written there would be
    indistinguishable from one the workflow declared, and would resume with it."""
    _res, run_dir = run(tmp_path, """
version: "2"
start: a
nodes:
  a:
    env: {AGENT_ROLE: triage}
    shell: "echo '<signal:ok>k</signal:ok>'"
    on_signal: {ok: __exit_ok__}
""")
    assert "AGENT_ROLE" not in (run_dir / "vars.yaml").read_text()


def test_each_pool_seat_gets_its_own(tmp_path):
    """Three harnesses in one pool, each needing its own role — the case node-level
    env cannot serve, because it is one value for the whole node."""
    _res, run_dir = run(tmp_path, """
version: "2"
start: p
nodes:
  p:
    max_parallel: 3
    env: {SHARED: node-level}
    inputs:
      - {slug: one, env: {AGENT_ROLE: qa}}
      - {slug: two, env: {AGENT_ROLE: security}}
      - {slug: three}
    shell: |
      echo "SEAT=$MEDULLA_INPUT_SLUG ROLE=[$AGENT_ROLE] SHARED=$SHARED"
      echo "<signal:ok>k</signal:ok>"
    on_signal: {__done__: __exit_ok__, __failed__: __exit_fail__}
""")
    out = bodies(run_dir)
    assert "SEAT=one ROLE=[qa]" in out, out
    assert "SEAT=two ROLE=[security]" in out, out
    assert "SEAT=three ROLE=[]" in out, "a seat without env inherited a neighbour's"
    assert out.count("SHARED=node-level") == 3, "node env must reach every seat"


def test_a_seat_env_is_not_turned_into_MEDULLA_INPUT_ENV(tmp_path):
    """Every other input field becomes MEDULLA_INPUT_<NAME>; `env` is the exception,
    because it names real variables rather than describing the seat."""
    _res, run_dir = run(tmp_path, """
version: "2"
start: p
nodes:
  p:
    inputs: [{slug: one, env: {ROLE_X: qa}}]
    shell: |
      echo "DIRECT=[$ROLE_X] INDIRECT=[$MEDULLA_INPUT_ENV]"
      echo "<signal:ok>k</signal:ok>"
    on_signal: {__done__: __exit_ok__, __failed__: __exit_fail__}
""")
    out = bodies(run_dir)
    assert "DIRECT=[qa]" in out, out
    assert "INDIRECT=[]" in out, out


@pytest.mark.parametrize("name", ["PATH", "HOME", "MEDULLA_RUN_ID", "PYTHONPATH"])
def test_reserved_names_are_refused_at_load(tmp_path, name):
    """A body that loses its PATH fails in a way nobody traces back to a yaml line."""
    (tmp_path / "workflow.yaml").write_text(
        'version: "2"\nstart: a\nnodes:\n  a:\n    env: {%s: x}\n'
        '    shell: "echo hi"\n    on_signal: {ok: __exit_ok__}\n' % name)
    res = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); "
         "from medulla.v2.cli import main; sys.exit(main(sys.argv[1:]))" % str(ROOT),
         "-w", str(tmp_path / "workflow.yaml"), "--validate"],
        capture_output=True, text=True, check=False)
    assert res.returncode != 0 and "reserved" in res.stdout + res.stderr


def test_the_panel_workflow_still_loads(tmp_path):
    """The contract gained a key; every shipped workflow must still parse."""
    wf = ROOT / "workflows/spar/workflow.yaml"
    assert pyyaml.safe_load(wf.read_text())["nodes"]


# ── values are templates ─────────────────────────────────────────────────────
#
# They were not rendered at all: the block was parsed at load time and merged raw, so a
# node written as REGION: "{{var:region}}" handed its body the literal six-character
# template. Reported by a lane converting three nodes off pre hooks, measured on 4.83.0:
# both {{var:ticket}} and {{vars.ticket}} arrived verbatim. Substitution is most of the
# reason to declare env on a node at all — handing a node its coordinate out of the run's
# variables — so these pin the rendering, and pin what must NOT be rendered with it.

def test_a_node_env_value_is_rendered(tmp_path):
    res, run_dir = run(tmp_path, """
version: "2"
start: a
vars: {ticket: T-42}
nodes:
  a:
    env: {TICKET: "{{var:ticket}}"}
    shell: 'echo "got=[$TICKET]"; echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
""")
    assert res.returncode == 0, res.stderr
    assert "got=[T-42]" in bodies(run_dir)


def test_a_literal_env_value_is_left_alone(tmp_path):
    res, run_dir = run(tmp_path, """
version: "2"
start: a
nodes:
  a:
    env: {ROLE: reviewer}
    shell: 'echo "got=[$ROLE]"; echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
""")
    assert res.returncode == 0, res.stderr
    assert "got=[reviewer]" in bodies(run_dir)


def test_an_env_value_may_render_empty(tmp_path):
    # A prompt that renders to nothing is a broken prompt; FLAG="" is a value. Rendering
    # env with required=True would fail the node for declaring an empty default.
    res, run_dir = run(tmp_path, """
version: "2"
start: a
nodes:
  a:
    env: {FLAG: "{{var:missing:-}}"}
    shell: 'echo "got=[$FLAG]"; echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
""")
    assert res.returncode == 0, res.stderr
    assert "got=[]" in bodies(run_dir)


def test_env_sees_a_var_its_own_pre_hook_set(tmp_path):
    # The rendering is lazy on purpose: env_fn is called again after the pre hook runs,
    # so a node can compute its own coordinate and then read it. Rendering once at node
    # entry would hand the body the value from BEFORE its own hook.
    res, run_dir = run(tmp_path, """
version: "2"
start: a
nodes:
  a:
    pre: 'echo "<signal:var key=lane>north</signal:var>"'
    env: {LANE: "{{var:lane}}"}
    shell: 'echo "got=[$LANE]"; echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
""")
    assert res.returncode == 0, res.stderr
    assert "got=[north]" in bodies(run_dir)


def test_a_seat_env_value_is_rendered_from_its_input(tmp_path):
    # A pool seat's env is where per-seat coordinates belong, and {{input.x}} is how a
    # seat names its own. Unrendered, every seat received the same six characters.
    res, run_dir = run(tmp_path, """
version: "2"
start: a
nodes:
  a:
    inputs:
      - {name: north, env: {LANE: "{{input.name}}"}}
      - {name: south, env: {LANE: "{{input.name}}"}}
    min_success: 2
    shell: 'echo "seat=[$LANE]"; echo "<signal:ok>k</signal:ok>"'
    on_signal: {__done__: __exit_ok__}
""")
    assert res.returncode == 0, res.stderr
    out = bodies(run_dir)
    assert "seat=[north]" in out and "seat=[south]" in out


def test_a_seat_env_still_beats_the_node_env_after_rendering(tmp_path):
    # Precedence is documented and was verified before rendering existed; rendering must
    # not reorder the merge.
    res, run_dir = run(tmp_path, """
version: "2"
start: a
vars: {who: node}
nodes:
  a:
    env: {OWNER: "{{var:who}}"}
    inputs:
      - {name: s1, env: {OWNER: "seat-{{input.name}}"}}
    min_success: 1
    shell: 'echo "owner=[$OWNER]"; echo "<signal:ok>k</signal:ok>"'
    on_signal: {__done__: __exit_ok__}
""")
    assert res.returncode == 0, res.stderr
    assert "owner=[seat-s1]" in bodies(run_dir)


def test_an_env_NAME_is_never_a_template(tmp_path):
    # Names are validated at load time against VAR_NAME_RE, and braces do not match it.
    # A name built from a variable would be a variable whose very existence depends on
    # runtime state — the failure would land in the body, not in the yaml.
    res, _ = run(tmp_path, """
version: "2"
start: a
nodes:
  a:
    env: {"{{var:name}}": x}
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
""")
    assert res.returncode != 0
    assert "invalid name" in (res.stderr + res.stdout)
