"""An agent's stdout must not be able to retire a value a later step trusts.

Every integrity anchor a workflow can build is a variable: a frozen contract digest,
a push destination, a working-tree fingerprint, computed by a shell step BEFORE any
agent runs precisely so the steps after can tell whether the thing they act on is the
thing that was checked. Until `sets`, one line of agent stdout replaced any of them,
exit 0, no warning.

Routing signals stay honoured — a step saying what happened is how a graph works.
This is only about handing a value to a later, different step.
"""
import json

from medulla.v2.engine import run_workflow
from test_hooks_agents import fake_script, read_run, setup


def test_agent_var_does_not_reach_the_next_step(tmp_path):
    """The reproduction from the ticket: shell sets ANCHOR, agent rewrites it,
    the shell after reads it back."""
    agent = fake_script(tmp_path, "rewriter.sh", """
echo "<signal:var key=ANCHOR>REWRITTEN-BY-THE-AGENT</signal:var>"
echo "<signal:done>ok</signal:done>"
""")
    yaml, work = setup(tmp_path, f"""
version: "2"
start: freeze
nodes:
  freeze:
    shell: |
      echo "<signal:var key=ANCHOR>frozen-by-the-author</signal:var>"
      echo "<signal:ready>ok</signal:ready>"
    on_signal: {{ready: middle}}
  middle:
    agent: {{harness: fake, model: "{agent}"}}
    prompt: "go"
    on_signal: {{done: check}}
  check:
    shell: |
      echo "ANCHOR=$ANCHOR" > seen.txt
      echo "<signal:ok>done</signal:ok>"
    on_signal: {{ok: __exit_ok__}}
""")
    run_workflow(yaml, workdir=work)
    _run, out, _j = read_run(yaml.parent)
    assert out["outcome"] == "succeeded"
    assert (work / "seen.txt").read_text().strip() == "ANCHOR=frozen-by-the-author"


def test_the_refusal_is_visible_in_the_run_log(tmp_path, capsys):
    """A silently dropped value costs the next person an hour."""
    agent = fake_script(tmp_path, "quiet.sh", """
echo "<signal:var key=ANCHOR>x</signal:var>"
echo "<signal:done>ok</signal:done>"
""")
    yaml, work = setup(tmp_path, f"""
version: "2"
start: a
vars: {{ANCHOR: original}}
nodes:
  a:
    agent: {{harness: fake, model: "{agent}"}}
    prompt: "go"
    on_signal: {{done: __exit_ok__}}
""")
    run_workflow(yaml, workdir=work)
    cap = capsys.readouterr()          # one call: readouterr drains the buffer
    printed = cap.out + cap.err
    assert "ignored var" in printed and "ANCHOR" in printed
    assert "agent.sets" in printed          # and it says how to allow it


def test_a_declared_var_is_honoured(tmp_path):
    agent = fake_script(tmp_path, "declared.sh", """
echo "<signal:var key=SCOPE>payments</signal:var>"
echo "<signal:var key=ANCHOR>sneaky</signal:var>"
echo "<signal:done>ok</signal:done>"
""")
    yaml, work = setup(tmp_path, f"""
version: "2"
start: a
vars: {{ANCHOR: frozen, SCOPE: unset}}
nodes:
  a:
    agent: {{harness: fake, model: "{agent}", sets: [SCOPE]}}
    prompt: "go"
    on_signal: {{done: b}}
  b:
    shell: |
      echo "SCOPE=$SCOPE ANCHOR=$ANCHOR" > seen.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {{ok: __exit_ok__}}
""")
    run_workflow(yaml, workdir=work)
    _run, out, _j = read_run(yaml.parent)
    assert out["outcome"] == "succeeded"
    # the declared one passes, the undeclared one does not — in the same stdout
    assert (work / "seen.txt").read_text().strip() == "SCOPE=payments ANCHOR=frozen"


def test_a_shell_body_still_sets_vars(tmp_path):
    """The field exists for shell steps; nothing about them changes."""
    yaml, work = setup(tmp_path, """
version: "2"
start: a
nodes:
  a:
    shell: |
      echo "<signal:var key=WHO>shell</signal:var>"
      echo "<signal:ready>k</signal:ready>"
    on_signal: {ready: b}
  b:
    shell: |
      echo "WHO=$WHO" > seen.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
""")
    run_workflow(yaml, workdir=work)
    assert (work / "seen.txt").read_text().strip() == "WHO=shell"


def test_the_post_hook_of_an_agent_node_still_sets_vars(tmp_path):
    """post is shell the author committed — it is the truth channel, not the agent."""
    agent = fake_script(tmp_path, "plain.sh", 'echo "<signal:done>ok</signal:done>"\n')
    yaml, work = setup(tmp_path, f"""
version: "2"
start: a
nodes:
  a:
    agent: {{harness: fake, model: "{agent}"}}
    prompt: "go"
    post: 'echo "<signal:var key=VERDICT>checked</signal:var>"'
    on_signal: {{done: b}}
  b:
    shell: |
      echo "VERDICT=$VERDICT" > seen.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {{ok: __exit_ok__}}
""")
    run_workflow(yaml, workdir=work)
    assert (work / "seen.txt").read_text().strip() == "VERDICT=checked"


def test_routing_signals_from_an_agent_are_untouched(tmp_path):
    """Only `var` is refused. A graph works because steps route it."""
    agent = fake_script(tmp_path, "router.sh", 'echo "<signal:take_b>go</signal:take_b>"\n')
    yaml, work = setup(tmp_path, f"""
version: "2"
start: a
nodes:
  a:
    agent: {{harness: fake, model: "{agent}"}}
    prompt: "go"
    on_signal: {{take_b: b, __failed__: __exit_fail__}}
  b:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {{ok: __exit_ok__}}
""")
    run_workflow(yaml, workdir=work)
    _run, out, _j = read_run(yaml.parent)
    assert out["outcome"] == "succeeded"
    _run, _o, journal = read_run(yaml.parent)
    assert [j["node"] for j in journal] == ["a", "b"]


def test_a_pool_agent_cannot_set_vars_either(tmp_path):
    """A pool is where agents run in bulk — and where a manifest row records what
    each one set. The gate is the same one, so this must hold there too."""
    agent = fake_script(tmp_path, "poolish.sh", """
echo "<signal:var key=ANCHOR>from-pool</signal:var>"
echo "<signal:done>ok</signal:done>"
""")
    yaml, work = setup(tmp_path, f"""
version: "2"
start: p
vars: {{ANCHOR: frozen}}
nodes:
  p:
    inputs: [one, two]
    agent: {{harness: fake, model: "{agent}"}}
    prompt: "{{{{input}}}}"
    on_signal: {{__done__: after, __failed__: __exit_fail__}}
  after:
    shell: |
      echo "ANCHOR=$ANCHOR" > seen.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {{ok: __exit_ok__}}
""")
    run_workflow(yaml, workdir=work)
    _run, out, _j = read_run(yaml.parent)
    assert out["outcome"] == "succeeded"
    assert (work / "seen.txt").read_text().strip() == "ANCHOR=frozen"
    run = sorted((yaml.parent / "runs").iterdir())[0]
    rows = [json.loads(l) for l in
            (run / "steps" / "001-p" / "manifest.jsonl").read_text().splitlines() if l.strip()]
    for row in rows:                       # the row must not claim it set something
        assert row["vars"] == {}


def test_sets_is_validated_at_load_time(tmp_path):
    """A typo in a security-relevant field must fail --validate, not mid-run."""
    from medulla.v2.contract import load_workflow
    from medulla.v2.errors import EngineCrash

    for bad, why in (("[1, 2]", "must be a list of var names"),
                     ("['not a name']", "not var names")):
        box = tmp_path / ("case" + str(abs(hash(bad)) % 1000))
        box.mkdir()
        yaml, _work = setup(box, f"""
version: "2"
start: a
nodes:
  a:
    agent: {{harness: fake, model: x, sets: {bad}}}
    prompt: "go"
    on_signal: {{done: __exit_ok__}}
""")
        try:
            load_workflow(yaml)
        except EngineCrash as exc:
            assert why in exc.message
        else:
            raise AssertionError(f"accepted agent.sets: {bad}")
