"""Named sessions: one conversation, several nodes.

Each CLI names its own handle and continues in its own way, so the adapter owns both
halves — finding the id in the output, and asking to continue from it.
"""
import json
from pathlib import Path

from conftest import fake_script, read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow
from medulla.v2.harness import resolve as resolve_harness
from medulla.v2.model import AgentSpec

# ── the adapters: id in, command out ─────────────────────────────────────────

def _build(harness: str, tmp_path: Path, resume=None):
    spec = AgentSpec(harness=harness, model=None)
    prompt = tmp_path / "p.md"
    prompt.write_text("go")
    return resolve_harness(spec).build(spec, prompt, "go", 60, resume=resume)


def test_claude_continues_by_flag(tmp_path):
    argv = _build("claude-code", tmp_path, resume="abc-123").argv
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "abc-123"
    assert "--resume" not in _build("claude-code", tmp_path).argv


def test_codex_continues_by_subcommand(tmp_path):
    """`codex exec resume <id>` — the word follows exec, and the sandbox flag must
    still land on exec, not be pushed behind the id where it reads as a prompt."""
    argv = _build("codex", tmp_path, resume="01a02ec7-d08d").argv
    assert argv[:4] == ["codex", "exec", "resume", "01a02ec7-d08d"]
    assert "--json" in argv and "--skip-git-repo-check" in argv
    assert "resume" not in _build("codex", tmp_path).argv


def test_opencode_continues_by_session_flag(tmp_path):
    argv = _build("opencode", tmp_path, resume="ses_fd138290").argv
    assert "--session" in argv and argv[argv.index("--session") + 1] == "ses_fd138290"


def test_agy_continues_by_conversation_flag_before_print(tmp_path):
    """--print consumes the NEXT token as the prompt, so anything after it is lost."""
    argv = _build("agy", tmp_path, resume="47adffb5").argv
    assert "--conversation" in argv
    assert argv.index("--conversation") < argv.index("--print")
    assert argv[argv.index("--conversation") + 1] == "47adffb5"


# ── the adapters: output in, id out ──────────────────────────────────────────

def test_each_adapter_mines_its_own_id_field():
    """Real lines, as the CLIs actually emit them (taken from run logs)."""
    cases = {
        "claude-code": ('{"type":"system","subtype":"hook_started",'
                        '"session_id":"15f505c7-274e-44fe-be5c-2dfed1201229"}',
                        "15f505c7-274e-44fe-be5c-2dfed1201229"),
        "codex": ('{"type":"thread.started","thread_id":"01a02ec7-d08d-7e11-9ce9"}',
                  "01a02ec7-d08d-7e11-9ce9"),
        "opencode": ('{"type":"step_start","sessionID":"ses_fd138290bffeGh575Ljgr87bGx"}',
                     "ses_fd138290bffeGh575Ljgr87bGx"),
        "agy": ('{"event":"init","conversation_id":"47adffb5-ff1e-4783-9075-12c311f36d3b"}',
                "47adffb5-ff1e-4783-9075-12c311f36d3b"),
    }
    for harness, (line, expected) in cases.items():
        adapter = resolve_harness(AgentSpec(harness=harness))
        assert adapter.session_id(line) == expected, harness
        assert adapter.session_id("nothing structured here") is None, harness


def test_the_id_is_found_when_the_line_is_prefixed():
    """Run logs prefix every line with [out]/[err]; the miner must still read it."""
    adapter = resolve_harness(AgentSpec(harness="agy"))
    assert adapter.session_id('[out] {"event":"init","conversation_id":"x-1"}') == "x-1"


def test_the_first_conversation_wins_when_several_are_reported():
    adapter = resolve_harness(AgentSpec(harness="claude-code"))
    stdout = ('{"session_id":"first"}\n{"session_id":"second"}\n')
    assert adapter.session_id(stdout) == "first"


# ── the engine: record, then hand back ───────────────────────────────────────

def test_a_second_node_continues_the_first_ones_conversation(tmp_path):
    """The fake harness reports an id like a real CLI, then proves it was given
    back by writing what it received."""
    agent = fake_script(tmp_path, "sess.sh", """
echo '{"session_id":"conv-7"}'
echo "$@" >> argv.log
echo "<signal:done>ok</signal:done>"
""")
    yaml, work = setup(tmp_path, f"""
version: "2"
start: first
nodes:
  first:
    agent: {{harness: fake, model: "{agent}", session: work}}
    prompt: "one"
    on_signal: {{done: second}}
  second:
    agent: {{harness: fake, model: "{agent}", session: work}}
    prompt: "two"
    on_signal: {{done: __exit_ok__}}
""")
    run_workflow(yaml, workdir=work)
    run, out, _j = read_run(yaml.parent)
    assert out["outcome"] == "succeeded"
    # recorded once, under the workflow's own name for it
    sessions = json.loads((run / "sessions.json").read_text())
    assert sessions["work"]["id"] == "conv-7"
    assert sessions["work"]["harness"] == "fake"

    # and the id actually reached the second invocation — recording it without
    # handing it back would pass every other assertion here
    calls = (work / "argv.log").read_text().splitlines()
    assert len(calls) == 2
    assert "--resume" not in calls[0]                    # the first turn opens it
    assert calls[1].endswith("--resume conv-7")          # the second continues it


def test_no_session_field_means_no_resume_and_no_file(tmp_path):
    """Every workflow written before this field keeps behaving exactly as it did."""
    agent = fake_script(tmp_path, "plain.sh", """
echo '{"session_id":"conv-9"}'
echo "<signal:done>ok</signal:done>"
""")
    yaml, work = setup(tmp_path, f"""
version: "2"
start: a
nodes:
  a:
    agent: {{harness: fake, model: "{agent}"}}
    prompt: "go"
    on_signal: {{done: __exit_ok__}}
""")
    run_workflow(yaml, workdir=work)
    run, _out, _j = read_run(yaml.parent)
    assert not (run / "sessions.json").exists()


def test_the_first_agent_keeps_the_name_when_two_claim_it(tmp_path):
    """A pool sharing one session name would otherwise let the last thread to
    finish decide which conversation the next node continues."""
    from medulla.v2.rundir import RunStore
    store = RunStore(tmp_path, "r1")
    store.record_session("panel", "first-id", "fake")
    store.record_session("panel", "second-id", "fake")
    assert store.session_id_for("panel") == "first-id"
    assert store.session_id_for("never-set") is None


def test_a_pool_gives_each_input_its_own_session(tmp_path):
    """Templated, so `panel-{{input}}` is one conversation per panelist."""
    agent = fake_script(tmp_path, "poolsess.sh", """
echo "{\\"session_id\\":\\"conv-$MEDULLA_INPUT\\"}"
echo "<signal:done>ok</signal:done>"
""")
    yaml, work = setup(tmp_path, f"""
version: "2"
start: p
nodes:
  p:
    inputs: [alpha, beta]
    agent: {{harness: fake, model: "{agent}", session: "panel-{{{{input}}}}"}}
    prompt: "{{{{input}}}}"
    on_signal: {{__done__: __exit_ok__, __failed__: __exit_fail__}}
""")
    run_workflow(yaml, workdir=work)
    run, out, _j = read_run(yaml.parent)
    assert out["outcome"] == "succeeded"
    sessions = json.loads((run / "sessions.json").read_text())
    assert sessions["panel-alpha"]["id"] == "conv-alpha"
    assert sessions["panel-beta"]["id"] == "conv-beta"


def test_a_session_cannot_move_between_harnesses(tmp_path):
    """A claude id handed to codex is a lookup that fails INSIDE the CLI, and shows
    up as an agent that simply did not answer. Fail where the name is still known."""
    from medulla.v2.errors import EngineCrash

    opener = fake_script(tmp_path, "opener.sh", """
echo '{"session_id":"conv-x"}'
echo "<signal:done>ok</signal:done>"
""")
    yaml, work = setup(tmp_path, f"""
version: "2"
start: first
nodes:
  first:
    agent: {{harness: fake, model: "{opener}", session: shared}}
    prompt: "one"
    on_signal: {{done: second}}
  second:
    agent: {{harness: claude-code, session: shared}}
    prompt: "two"
    on_signal: {{done: __exit_ok__}}
""")
    try:
        run_workflow(yaml, workdir=work)
    except EngineCrash as exc:
        assert "cannot move between CLIs" in exc.message
        assert "'fake'" in exc.message and "claude-code" in exc.message
    else:
        run, out, _j = read_run(yaml.parent)
        assert out["outcome"] == "crashed", "a cross-harness resume must not proceed"


