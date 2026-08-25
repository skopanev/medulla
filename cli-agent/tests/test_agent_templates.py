"""Every scalar agent field is a template.

An ensemble is just a pool with a per-input harness, which only works if harness, model
and effort render like anything else.
"""
import json

from medulla.v2.engine import run_workflow

from conftest import fake_script, read_run, write_workflow as setup

def test_agent_fields_are_templates(tmp_path):
    # contract: every scalar action field is a template — model via {{var:}}
    script = fake_script(tmp_path, "tpl.sh", 'echo "arg2=$2" > args.txt\necho "<signal:ok>k</signal:ok>"\n')
    text = f"""
version: "2"
start: a
vars: {{SCRIPT: "{script}"}}
nodes:
  a:
    agent: {{harness: fake, model: "{{{{var:SCRIPT}}}}", args: ["{{{{var:MODE:-fast}}}}"]}}
    prompt: "p"
    on_signal: {{ok: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert (work / "args.txt").read_text().strip() == "arg2=fast"


def test_agent_without_prompt_is_validation_error(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    agent: codex
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1
    assert not (path.parent / "runs").exists()       # E_VALIDATION: before any run dir


def test_post_var_overrides_body_var(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: |
      echo "<signal:var key=V>body</signal:var>"
      echo "<signal:ok>k</signal:ok>"
    post: 'echo "<signal:var key=V>post</signal:var>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    assert "V: post" in (run / "vars.yaml").read_text()


def test_pre_and_post_together(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    pre: 'echo "<signal:var key=N>3</signal:var>"'
    shell: 'echo "{{var:N}}" > out.txt; echo "<signal:ok>k</signal:ok>"'
    post: '[ "$(cat out.txt)" = "3" ]'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0


def test_pool_pre_sees_the_rendered_harness_not_the_template(tmp_path):
    # In a pool `harness:` is itself a template. MEDULLA_HARNESS used to carry the raw
    # "{{input.harness}}", so a pre hook gating on the harness matched nothing and the
    # guard silently never fired. It must arrive rendered, per input.
    text = """
version: "2"
start: a
nodes:
  a:
    inputs:
      - {slug: one, harness: fake}
    pre: 'echo "SAW=$MEDULLA_HARNESS"'
    agent:
      harness: "{{input.harness}}"
    prompt: "hi"
    on_signal: {__done__: __exit_ok__, __failed__: __exit_fail__}
"""
    path, work = setup(tmp_path, text)
    run_workflow(path, workdir=work)
    pre_log = next((path.parent / "runs").glob("*/steps/001-a/input-0001/pre.txt"))
    assert "SAW=fake" in pre_log.read_text()
    assert "{{input.harness}}" not in pre_log.read_text()


def test_pool_pre_failure_drops_only_that_input(tmp_path):
    # The panel contract: a panelist whose provider is dead sits the round out while the
    # others still deliver. A pre failure must stay a SOFT fail (attempts=0), never a crash.
    import json
    script = fake_script(tmp_path, "worker.sh", 'exit 0\n')
    text = f"""
version: "2"
start: a
nodes:
  a:
    inputs:
      - {{slug: alive}}
      - {{slug: dead}}
    max_parallel: all
    min_success: 1
    pre: '[ "$MEDULLA_INPUT_SLUG" = dead ] && {{ echo "dead sits out" >&2; exit 1; }} || exit 0'
    agent: {{harness: fake, model: "{script}"}}
    prompt: "hi"
    on_signal: {{__done__: __exit_ok__, __failed__: __exit_fail__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0          # the pool still concludes
    manifest = next((path.parent / "runs").glob("*/steps/001-a/manifest.jsonl"))
    rows = [json.loads(l) for l in manifest.read_text().splitlines()]
    dead = [r for r in rows if not r["ok"]]
    assert len(dead) == 1
    assert dead[0]["reason"] == "pre" and dead[0]["attempts"] == 0
    assert any(r["ok"] for r in rows)                     # the healthy one delivered


def test_a_node_is_told_where_its_workflow_lives(tmp_path):
    """A node that wants to run its workflow's own script had no way to find it: the
    path depends on which copy resolved and, in a container, on where it was mounted."""
    yaml, work = setup(tmp_path, """
version: "2"
start: a
nodes:
  a:
    shell: |
      echo "$MEDULLA_WORKFLOW_DIR" > seen.txt
      test -f "$MEDULLA_WORKFLOW_DIR/workflow.yaml"
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
""")
    run_workflow(yaml, workdir=work)
    _run, out, _j = read_run(yaml.parent)
    assert out["outcome"] == "succeeded"          # the `test -f` above had to pass
    assert (work / "seen.txt").read_text().strip() == str(yaml.parent)
