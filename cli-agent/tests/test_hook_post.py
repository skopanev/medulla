"""The post hook: the truth channel.

A body claiming success proves nothing; post is the shell the author committed to\ncheck the artifact, and its verdict outranks the body's own.
"""
import json

from conftest import fake_script, read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow

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
    assert run_workflow(path, workdir=work) == 2
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
    assert run_workflow(path, workdir=work) == 0
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
    assert run_workflow(path, workdir=work) == 2
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
    assert run_workflow(path, workdir=work) == 0


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
    assert run_workflow(path, workdir=work) == 0
    assert (work / "post-env.txt").read_text().strip() == "0 go"




def test_post_veto_does_not_report_the_body_as_dead(tmp_path):
    """A body that exited 0 did not die. The manifest used to say "body died: rc=0"
    whenever post vetoed — a contradiction that sent readers hunting a crash that
    never happened while the complete artifact sat on disk. A full-store sweep found
    43 delivered artifacts recorded ok=false, 35 of them carrying rc=0.
    """
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    post: 'echo "artifact has no VERDICT" >&2; exit 1'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    _, outcome, journal = read_run(path.parent)
    msg = journal[0]["message"]
    assert "body died" not in msg, msg
    assert "post hook vetoed" in msg, msg
    assert "body rc=0" in msg, msg
    assert "artifact has no VERDICT" in msg, msg


def test_a_dead_body_keeps_its_own_stderr_in_the_message(tmp_path):
    """When the body itself died, ITS stderr is the evidence — a credential broker's
    stack trace, an OAuth prompt that timed out. The post hook only noticed the missing
    artifact afterwards, so naming the hook there would replace the cause with its
    symptom. Live: three panelists failed this way in one round, and the broker trace
    in the message is the only thing that said why.
    """
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "cannot reach the broker: SSL EOF" >&2; exit 1'
    post: 'echo "no artifact written" >&2; exit 1'
    ignore_exit_code: false
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    _, outcome, journal = read_run(path.parent)
    msg = journal[0]["message"]
    assert "cannot reach the broker" in msg, msg
    assert "post hook vetoed" not in msg, msg
