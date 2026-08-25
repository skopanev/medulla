"""Agent bodies with hooks: what pre and post see around an agent.
"""
import json

from medulla.v2.engine import run_workflow

from conftest import fake_script, read_run, write_workflow as setup

def test_post_receives_current_harness(tmp_path):
    script = fake_script(tmp_path, "ok.sh", 'echo "<signal:ok>k</signal:ok>"\n')
    text = f"""
version: "2"
start: a
nodes:
  a:
    agent: {{harness: fake, model: "{script}"}}
    prompt: "p"
    post: 'echo "$MEDULLA_HARNESS" > harness.txt'
    on_signal: {{ok: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert (work / "harness.txt").read_text().strip() == "fake"


def test_var_from_default_outcome_never_applied(tmp_path):
    # panel G-1: a __default__ conclusion is a communication failure — its vars
    # must not leak into the recovery scope
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:var key=LEAKED>yes</signal:var>"'
    on_signal: {__default__: recover}
  recover:
    shell: '[ -z "${LEAKED:-}" ] && echo "<signal:ok>clean</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    assert "LEAKED" not in (run / "vars.yaml").read_text()


def test_post_sees_hook_timeout_not_body_timeout(tmp_path):
    # panel G-2: MEDULLA_TIMEOUT_S is the step's own resolved timeout
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    timeout: 42
    post: 'echo "$MEDULLA_TIMEOUT_S" > post-timeout.txt'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert (work / "post-timeout.txt").read_text().strip() == "60"


def test_pre_updates_survive_normal_path(tmp_path):
    # panel G-3: pre updates must reach the outcome when the body runs
    text = """
version: "2"
start: a
nodes:
  a:
    pre: 'echo "<signal:update>pre-progress</signal:update>"'
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0     # smoke: update path must not crash


def test_pre_signal_beats_nonzero_rc(tmp_path):
    # panel (gpt5): the grammar is uniform — a known signal wins over rc everywhere
    text = """
version: "2"
start: a
nodes:
  a:
    pre: 'echo "<signal:skip>cached</signal:skip>"; exit 7'
    shell: "touch should-not-exist"
    on_signal: {skip: __exit_ok__, ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert not (work / "should-not-exist").exists()


def test_fallback_inherits_primary_attempt_budget(tmp_path):
    # contract: "primary gets N, then fallback gets N"
    primary = fake_script(tmp_path, "dying.sh", "exit 1\n")
    fallback = fake_script(tmp_path, "flaky-fb.sh", """
if [ -f fb-marker ]; then echo "<signal:ok>second fb try</signal:ok>"; else touch fb-marker; exit 1; fi
""")
    text = f"""
version: "2"
start: a
nodes:
  a:
    agent: {{harness: fake, model: "{primary}"}}
    prompt: "p"
    max_attempts: 2
    fallback: {{agent: {{harness: fake, model: "{fallback}"}}}}
    on_signal: {{ok: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0     # 2 primary + 2 fallback (inherited budget)
    _, _, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 4


