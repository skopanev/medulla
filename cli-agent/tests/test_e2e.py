"""End-to-end walking skeleton: real subprocesses, real run dirs, adversarial cases."""
import json

import pytest

from medulla.v2.engine import run_pipeline


def setup_pipeline(tmp_path, text):
    pdir = tmp_path / "pipe"
    pdir.mkdir()
    (pdir / "pipeline.yaml").write_text(text, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    return pdir / "pipeline.yaml", work


def read_run(pdir):
    runs = sorted((pdir / "runs").iterdir())
    assert len(runs) == 1, f"expected one run dir, got {runs}"
    run = runs[0]
    outcome = json.loads((run / "outcome.json").read_text())
    journal_path = run / "journal.jsonl"
    journal = (
        [json.loads(l) for l in journal_path.read_text().splitlines()]
        if journal_path.exists() else []
    )
    return run, outcome, journal


def test_happy_path_vars_and_routing(tmp_path):
    text = """
version: "2"
start: a
vars: {GREETING: hello}
nodes:
  a:
    shell: |
      echo "<signal:var key=TARGET>world</signal:var>"
      echo "<signal:go>ready</signal:go>"
    on_signal: {go: b}
  b:
    shell: |
      echo "computed: {{var:GREETING}} $TARGET"
      [ "$TARGET" = "world" ] && echo "<signal:ok>done</signal:ok>"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    run, outcome, journal = read_run(path.parent)
    assert outcome["outcome"] == "succeeded" and outcome["steps"] == 2
    assert [r["node"] for r in journal] == ["a", "b"]
    assert journal[0]["signal"] == "go" and journal[1]["next"] == "__exit_ok__"
    vars_yaml = (run / "vars.yaml").read_text()
    assert "TARGET: world" in vars_yaml
    # config snapshot is immutable input for resume
    assert (run / "pipeline.yaml").read_text() == text


def test_known_signal_beats_nonzero_exit(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: |
      echo "<signal:go>said it</signal:go>"
      exit 3
    on_signal: {go: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    _, outcome, journal = read_run(path.parent)
    assert journal[0]["rc"] == 3 and journal[0]["signal"] == "go"


def test_stderr_never_routes(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: |
      echo "<signal:go>from stderr</signal:go>" >&2
    on_signal: {go: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 2       # silence -> __default__ -> __exit_fail__
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["signal"] == "__default__"


def test_body_death_is_failed_builtin(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "exit 5"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 2
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["signal"] == "__failed__"
    assert "rc=5" in outcome["error"]["message"]


def test_failed_can_be_rerouted_to_recovery(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "exit 5"
    on_signal: {__failed__: recover}
  recover:
    shell: 'echo "<signal:ok>saved: $MEDULLA_LAST_MESSAGE</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0


def test_mechanical_retry_within_attempts(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: |
      if [ -f marker ]; then echo "<signal:ok>second try</signal:ok>"; else touch marker; exit 1; fi
    max_attempts: 2
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    _, _, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 2


def test_shell_silence_not_retried(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "true"
    max_attempts: 3
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 2
    _, outcome, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 1                 # deterministic silence: no retry
    assert outcome["error"]["signal"] == "__default__"


def test_timeout_becomes_rc_124_then_failed(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "sleep 5"
    timeout: 1
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 2
    _, outcome, journal = read_run(path.parent)
    assert journal[0]["rc"] == 124 and journal[0]["timed_out"] is True
    assert outcome["error"]["signal"] == "__failed__"


def test_last_message_flows_to_next_node(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:go>payload-42</signal:go>"'
    on_signal: {go: b}
  b:
    shell: |
      echo "env=$MEDULLA_LAST_MESSAGE tmpl={{last.message}}" > received.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    received = (work / "received.txt").read_text()
    assert "env=payload-42" in received and "tmpl=payload-42" in received


def test_node_start_override(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "exit 7"
    on_signal: {ok: __exit_ok__}
  b:
    shell: 'echo "<signal:ok>skipped a</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work, start_override="b") == 0


def test_var_from_failed_attempt_not_applied(tmp_path):
    # fold law: state signals apply atomically from the CONCLUDING attempt only
    text = """
version: "2"
start: a
nodes:
  a:
    shell: |
      if [ -f marker ]; then
        echo "<signal:ok>k</signal:ok>"
      else
        touch marker
        echo "<signal:var key=POISON>from-failed-attempt</signal:var>"
        exit 1
      fi
    max_attempts: 2
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    assert "POISON" not in (run / "vars.yaml").read_text()


def test_pool_crashes_as_not_implemented_yet(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    inputs: [x]
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 1       # E_INTERNAL crash path
    _, outcome, _ = read_run(path.parent)
    assert outcome["outcome"] == "crashed" and outcome["error"]["code"] == "E_INTERNAL"


def test_rendered_empty_shell_is_e_render(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "{{var:MISSING:-}}"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 1
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_RENDER"


def test_body_cannot_spoof_engine_facts(tmp_path):
    # a body printing <signal:__failed__> at rc=0 must NOT route the recovery edge
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:__failed__>spoof</signal:__failed__>"'
    on_signal: {__failed__: recover}
  recover:
    shell: 'echo "<signal:ok>reached</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 2       # silence -> __default__, not recover
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["signal"] == "__default__"


def test_deadline_exhaustion_is_e_deadline(tmp_path):
    text = """
version: "2"
start: a
timeout: 1
nodes:
  a:
    shell: "sleep 3"
    on_signal: {__failed__: b}
  b:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 1       # b never runs: budget is gone
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_DEADLINE"


def test_fast_child_under_small_remaining_budget(tmp_path):
    # clamp, not crash: a consumes most of a 2s budget, b still gets its chance
    text = """
version: "2"
start: a
timeout: 2
nodes:
  a:
    shell: |
      sleep 0.8
      echo "<signal:go>k</signal:go>"
    on_signal: {go: b}
  b:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0


def test_env_exposes_timeout_and_last_event_json(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:go>payload</signal:go>"'
    on_signal: {go: b}
  b:
    shell: |
      echo "t=$MEDULLA_TIMEOUT_S j=$MEDULLA_LAST_EVENT_JSON" > env.txt
      echo "<signal:ok>k</signal:ok>"
    timeout: 42
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    env_txt = (work / "env.txt").read_text()
    assert "t=42" in env_txt and '"node"' in env_txt and "payload" in env_txt


def test_shell_retry_exhaustion_routes_failed(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "exit 1"
    max_attempts: 2
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 2
    _, outcome, journal = read_run(path.parent)
    assert journal[0]["attempts"] == 2
    assert outcome["error"]["signal"] == "__failed__"


def test_defaults_on_signal_tier_routing(tmp_path):
    text = """
version: "2"
start: a
defaults:
  on_signal: {go: b}
nodes:
  a:
    shell: 'echo "<signal:go>k</signal:go>"'
    on_signal: {}
  b:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__, go: __exit_fail__}   # override: defaults self-edge is illegal
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0


def test_unwired_real_harness_is_e_internal(tmp_path):
    # panel-held razor: E_HARNESS = binary missing/unresolvable ONLY;
    # "not wired yet" is an engine limitation -> E_INTERNAL
    text = """
version: "2"
start: a
nodes:
  a:
    agent: codex
    prompt: "p"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 1
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_INTERNAL"


def test_run_id_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDULLA_RUN_ID", "corr1234")
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    runs = list((path.parent / "runs").iterdir())
    assert runs[0].name.endswith("-corr1234")


def test_validation_error_before_run_dir(tmp_path):
    text = 'version: "2"\nstart: nope\nnodes:\n  a:\n    shell: "true"\n    on_signal: {ok: __exit_ok__}\n'
    path, work = setup_pipeline(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 1
    assert not (path.parent / "runs").exists()         # crash before any run dir
