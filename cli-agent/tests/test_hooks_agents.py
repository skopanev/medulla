"""Part 2: pre/post hooks, attempts+fallback, fake-harness agent bodies."""
import json

from medulla.v2.engine import run_pipeline


def setup(tmp_path, text):
    pdir = tmp_path / "pipe"
    pdir.mkdir()
    (pdir / "pipeline.yaml").write_text(text, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    return pdir / "pipeline.yaml", work


def read_run(pdir):
    runs = sorted((pdir / "runs").iterdir())
    run = runs[0]
    outcome = json.loads((run / "outcome.json").read_text())
    jp = run / "journal.jsonl"
    journal = [json.loads(l) for l in jp.read_text().splitlines()] if jp.exists() else []
    return run, outcome, journal


def fake_script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    return str(p)


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
    assert run_pipeline(path, workdir=work) == 0


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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 2
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
    assert run_pipeline(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    assert "CACHE: hit" in (run / "vars.yaml").read_text()


# ── post ─────────────────────────────────────────────────────────────────────

def test_post_veto_consumes_attempts_then_failed(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    post: "exit 1"
    max_attempts: 2
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 2
    _, outcome, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 2
    assert outcome["error"]["signal"] == "__failed__"


def test_post_retry_until_artifact(tmp_path):
    # body always signals; post gates on an artifact that appears on attempt 2
    text = """
version: "2"
start: a
nodes:
  a:
    shell: |
      echo x >> counter
      echo "<signal:ok>k</signal:ok>"
    post: '[ "$(wc -l < counter)" -ge 2 ]'
    max_attempts: 3
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    _, _, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 2


def test_post_override_signal(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:planned>looks done</signal:planned>"'
    post: 'echo "<signal:needs_rework>plan is garbage</signal:needs_rework>"'
    on_signal: {planned: __exit_ok__, needs_rework: __exit_fail__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 2
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["signal"] == "needs_rework"
    assert outcome["error"]["message"] == "plan is garbage"


def test_post_silent_keeps_body_signal(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    post: "true"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0


def test_post_sees_body_rc_and_signal(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:go>k</signal:go>"'
    post: 'echo "$MEDULLA_BODY_RC $MEDULLA_BODY_SIGNAL" > post-env.txt'
    on_signal: {go: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    assert (work / "post-env.txt").read_text().strip() == "0 go"


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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 2          # silence -> __default__
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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 1
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
    assert run_pipeline(path, workdir=work) == 2
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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 0


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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 0     # smoke: update path must not crash


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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 0     # 2 primary + 2 fallback (inherited budget)
    _, _, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 4


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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 1
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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 0
