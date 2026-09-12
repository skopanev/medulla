"""What a retried agent is told about the attempt it is replacing.

A post hook that vetoes an answer used to keep the reason to itself: it reached the
manifest and the journal, and the next attempt re-read the identical prompt. Over the
stored corpus that cost real money — of 662 retries, 310 ended in the SAME complaint as
the attempt they replaced. These tests pin the reason into the prompt the agent reads,
and pin the three things that must NOT change with it.
"""
import pytest
from conftest import fake_script, read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow
from medulla.v2.engine_message import retry_note


# ── the note itself (pure) ──────────────────────────────────────────────────

def test_note_carries_what_the_hook_said():
    note = retry_note("1.p1", 1, "FINDINGS section is missing")
    assert "FINDINGS section is missing" in note
    assert "1.p1" in note


def test_no_note_when_the_body_died():
    # post_rc 0 (or None): the hook did not veto — whatever went wrong, it is not
    # something the agent can be told about, because the agent never answered.
    assert retry_note("1.p1", 0, "irrelevant") is None
    assert retry_note("1.p1", None, "") is None


def test_no_note_when_the_hook_vetoed_silently():
    # rc alone is not a reason. Handing the agent "rc=1" invites it to guess, and a
    # guess is exactly what the note exists to prevent.
    assert retry_note("1.p1", 1, "   \n  ") is None


# ── end to end ──────────────────────────────────────────────────────────────

VETO_ONCE = '''
if [ ! -f vetoed ]; then
  touch vetoed
  echo "you wrote no FINDINGS heading" >&2
  exit 1
fi
'''

RECORD_PROMPT = '''
n=$(cat count 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > count
cp "$1" "seen-$n.md"
echo "<signal:ok>done</signal:ok>"
'''


def _run(tmp_path, monkeypatch, post=VETO_ONCE, body=RECORD_PROMPT, session=""):
    monkeypatch.setenv("MEDULLA_RETRY_DELAY_S", "0")
    script = fake_script(tmp_path, "agent.sh", body)
    hook = chr(10).join("      " + l for l in post.strip().splitlines())
    text = f"""
version: "2"
start: a
nodes:
  a:
    agent:
      harness: fake
      model: "{script}"
{session}    prompt: "the original task"
    max_attempts: 2
    post: |
{hook}
    on_signal: {{ok: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    rc = run_workflow(path, workdir=work)
    return rc, work, path


def test_the_retry_is_told_why(tmp_path, monkeypatch):
    rc, work, _ = _run(tmp_path, monkeypatch)
    assert rc == 0
    first = (work / "seen-1.md").read_text()
    second = (work / "seen-2.md").read_text()
    assert "you wrote no FINDINGS heading" not in first, "nothing to report yet"
    assert "you wrote no FINDINGS heading" in second, "the veto must reach the agent"


def test_the_task_survives_the_note(tmp_path, monkeypatch):
    # The note is an addition, never a replacement: an agent told only what it did
    # wrong, without the job, answers a question nobody asked.
    _, work, _ = _run(tmp_path, monkeypatch)
    assert "the original task" in (work / "seen-2.md").read_text()


def test_the_signal_protocol_survives_the_note(tmp_path, monkeypatch):
    # The protocol rides at the END of whatever text reaches the agent. Inserting the
    # note after it would bury the one part the engine needs the agent to obey.
    _, work, _ = _run(tmp_path, monkeypatch)
    text = (work / "seen-2.md").read_text()
    assert "<signal:" in text
    assert text.index("you wrote no FINDINGS heading") < text.rindex("<signal:")


def test_both_prompts_are_kept_on_disk(tmp_path, monkeypatch):
    # The retry's prompt is a different document; overwriting prompt.md with it would
    # erase the evidence of what the first attempt was actually asked.
    _, _work, path = _run(tmp_path, monkeypatch)
    run, _, _ = read_run(path.parent)
    step = next(d for d in (run / "steps").iterdir() if d.is_dir())
    names = sorted(p.name for p in step.glob("prompt*.md"))
    assert "prompt.md" in names
    assert any(n.startswith("prompt-retry-") for n in names), names


def test_a_body_that_died_gets_no_note(tmp_path, monkeypatch):
    # rc!=0 from the body with a passing hook: nothing to pass on, and the prompt must
    # come back byte for byte — a spurious note would change the input on every retry
    # of every flaky harness in the fleet.
    body = '''
n=$(cat count 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > count
cp "$1" "seen-$n.md"
if [ "$n" = "1" ]; then exit 1; fi
echo "<signal:ok>done</signal:ok>"
'''
    rc, work, _ = _run(tmp_path, monkeypatch, post="exit 0", body=body)
    assert rc == 0
    assert (work / "seen-1.md").read_text() == (work / "seen-2.md").read_text()


def test_the_retry_stays_in_the_same_conversation(tmp_path, monkeypatch):
    # Rebuilding the body to add the note re-reads the session store — which THIS
    # attempt may have just written to. Recomputing the resume id there would move the
    # retry into a conversation the first attempt opened mid-node. It must not.
    body = '''
n=$(cat count 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > count
echo "$@" > argv-$n.txt
cp "$1" "seen-$n.md"
echo '{"session_id":"opened-by-attempt-1"}'
echo "<signal:ok>done</signal:ok>"
'''
    _, work, _ = _run(tmp_path, monkeypatch, body=body,
                      session="      session: chat\n")
    assert "--resume" not in (work / "argv-2.txt").read_text(), \
        "the retry must not jump into a conversation this node just opened"
