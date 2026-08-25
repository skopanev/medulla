"""Agent bodies: attempts, fallback, and the prompt each one gets.

The contract is 'primary gets N, then fallback gets N', and a fallback without its own
budget inherits the primary's — including the prompt it was given.
"""
import json

from conftest import fake_script, read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow

# ── agent bodies via the fake harness ────────────────────────────────────────

def test_fake_agent_happy_path_and_prompt_render(tmp_path):
    script = fake_script(tmp_path, "agent.sh", """
grep -q "hello world" "$1" || exit 9
echo "<signal:ok>from agent</signal:ok>"
""")
    text = f"""
version: "2"
start: a
vars: {{GREETING: "hello world"}}
nodes:
  a:
    agent: {{harness: fake, model: "{script}"}}
    prompt: "Say: {{{{var:GREETING}}}}"
    on_signal: {{ok: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, journal = read_run(path.parent)
    prompt = (run / "steps" / "001-a" / "prompt.md").read_text()
    assert prompt.startswith("Say: hello world")
    assert "Signal protocol" in prompt          # the engine delivers the syntax
    assert journal[0]["harness"] == "fake"


def test_agent_silence_retries_primary_never_fallback(tmp_path):
    primary = fake_script(tmp_path, "silent.sh", 'echo run >> silent-invocations\nexit 0\n')
    fallback = fake_script(tmp_path, "fb.sh", 'touch fallback-ran\necho "<signal:ok>k</signal:ok>"\n')
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
    assert run_workflow(path, workdir=work) == 2          # silence -> __default__
    assert (work / "silent-invocations").read_text().count("run") == 2
    assert not (work / "fallback-ran").exists()           # silence NEVER falls back
    _, outcome, journal = read_run(path.parent)
    assert outcome["error"]["signal"] == "__default__"
    assert journal[0]["fallback"] is False


def test_agent_rc_failure_switches_to_fallback(tmp_path):
    primary = fake_script(tmp_path, "dying.sh", "exit 1\n")
    fallback = fake_script(tmp_path, "fb.sh", 'echo "<signal:ok>saved</signal:ok>"\n')
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
    assert run_workflow(path, workdir=work) == 0
    _, _, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 3                    # 2 primary + 1 fallback
    assert journal[0]["fallback"] is True


def test_fallback_inherits_primary_prompt(tmp_path):
    primary = fake_script(tmp_path, "dying.sh", "exit 1\n")
    fallback = fake_script(tmp_path, "fb.sh", 'echo "<signal:ok>k</signal:ok>"\n')
    text = f"""
version: "2"
start: a
vars: {{TOPIC: quarks}}
nodes:
  a:
    agent: {{harness: fake, model: "{primary}"}}
    prompt: "Explain {{{{var:TOPIC}}}}"
    fallback: {{agent: {{harness: fake, model: "{fallback}"}}}}
    on_signal: {{ok: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    step = run / "steps" / "001-a"
    assert (step / "prompt.md").read_text() == (step / "prompt-fallback.md").read_text()


def test_unknown_harness_is_e_harness(tmp_path):
    # part 5 wired all real harnesses; only an unknown NAME is unresolvable here
    text = """
version: "2"
start: a
nodes:
  a:
    agent: {harness: nonsense, model: x}
    prompt: "p"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_HARNESS"


def test_attempt_ids_are_distinct(tmp_path):
    script = fake_script(tmp_path, "ids.sh", 'echo "$MEDULLA_ATTEMPT_ID" >> ids.txt\nexit 1\n')
    text = f"""
version: "2"
start: a
nodes:
  a:
    agent: {{harness: fake, model: "{script}"}}
    prompt: "p"
    max_attempts: 2
    on_signal: {{__failed__: __exit_fail__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    ids = (work / "ids.txt").read_text().split()
    assert ids == ["001.p1", "001.p2"]


def test_silent_agent_with_failing_post_can_fallback(tmp_path):
    # emergent semantics pinned: bare silence never falls back, but silence + a
    # failing post is a mechanical failure — the truth channel justifies a model switch
    primary = fake_script(tmp_path, "lazy.sh", "exit 0\n")   # silent, writes nothing
    fallback = fake_script(tmp_path, "worker.sh",
                           'touch artifact\necho "<signal:ok>k</signal:ok>"\n')
    text = f"""
version: "2"
start: a
nodes:
  a:
    agent: {{harness: fake, model: "{primary}"}}
    prompt: "p"
    post: "test -f artifact"
    max_attempts: 1
    fallback: {{agent: {{harness: fake, model: "{fallback}"}}}}
    on_signal: {{ok: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    _, _, journal = read_run(path.parent)
    assert journal[0]["fallback"] is True


def test_fallback_prompt_does_not_see_failed_primary_vars(tmp_path):
    # panel trap: a var emitted by a DYING primary attempt is never applied
    # (fold law), so the fallback prompt renders with the pre-node value
    primary = fake_script(tmp_path, "dying.sh",
                          'echo "<signal:var key=X>leaked</signal:var>"\nexit 1\n')
    fallback = fake_script(tmp_path, "fb.sh",
                           'grep -q "X=none" "$1" && echo "<signal:ok>k</signal:ok>"\n')
    text = f"""
version: "2"
start: a
nodes:
  a:
    agent: {{harness: fake, model: "{primary}"}}
    prompt: "p"
    fallback:
      agent: {{harness: fake, model: "{fallback}"}}
      prompt: "X={{{{var:X:-none}}}}"
    on_signal: {{ok: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0


