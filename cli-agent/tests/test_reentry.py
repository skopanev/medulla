"""Re-entering a FINISHED run at a named node.

The shape from medulla-wufuwoga1f: an agent works, a host step acts on the result, and
the answer comes back wrong — a protected branch refuses the push, a rebase conflicts.
Going back to that agent beats starting over, because it knows what it changed and why.

Until now a finished run refused: RESUMABLE_OUTCOMES is interrupted/crashed, and the
advice was to delete outcome.json — after which what a resume would do was undefined.
"""
import json

from conftest import read_run, write_workflow
from medulla.v2.engine import run_workflow

TWO_STEPS = """
version: "2"
start: work
nodes:
  work:
    shell: |
      echo "pass $(cat passes 2>/dev/null | wc -l | tr -d ' ')" >> passes
      echo "<signal:done>ok</signal:done>"
    on_signal: {done: check}
  check:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""


def test_a_finished_run_can_be_re_entered_at_a_node(tmp_path):
    yaml, work = write_workflow(tmp_path, TWO_STEPS)
    run_workflow(yaml, workdir=work)
    run, out, journal = read_run(yaml.parent)
    assert out["outcome"] == "succeeded"
    assert len(journal) == 2

    rc = run_workflow(yaml, workdir=work, resume_dir=run, start_override="work")
    assert rc == 0
    _run, out2, journal2 = read_run(yaml.parent)
    assert out2["outcome"] == "succeeded"
    assert len(journal2) == 4                  # both passes are on the record
    assert (work / "passes").read_text().count("pass") == 2


def test_without_a_node_a_finished_run_still_refuses(tmp_path):
    """Resuming a run that ended is undefined; re-entering it somewhere is not."""
    yaml, work = write_workflow(tmp_path, TWO_STEPS)
    run_workflow(yaml, workdir=work)
    run, _out, _j = read_run(yaml.parent)

    assert run_workflow(yaml, workdir=work, resume_dir=run) == 1
    assert (run / "outcome.json").exists()     # untouched: nothing was re-run


def test_an_interrupted_run_resumes_as_before(tmp_path):
    """The old path is unchanged: no node given, and it picks up where it stopped."""
    yaml, work = write_workflow(tmp_path, TWO_STEPS)
    run_workflow(yaml, workdir=work)
    run, _out, _j = read_run(yaml.parent)
    outcome = json.loads((run / "outcome.json").read_text())
    outcome["outcome"] = "interrupted"
    (run / "outcome.json").write_text(json.dumps(outcome))

    assert run_workflow(yaml, workdir=work, resume_dir=run) == 0
