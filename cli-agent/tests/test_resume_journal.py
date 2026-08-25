"""Resuming: what the journal replays, and what it refuses to lose.

A truncated tail must not cost the steps written before it, and a step that already
succeeded must not run twice.
"""
import fcntl
import json
import os
from pathlib import Path

from medulla.v2.cli import main as cli_main
from medulla.v2.engine import find_resumable, run_workflow

from conftest import DECISION_RESUME, POOL_RESUME, read_outcome, runs_of, work_dir, write_workflow as setup

def test_pool_resume_no_resource_no_rerun(tmp_path):
    # THE dangerous scenario: pool dies on deadline mid-flight; resume must
    # (1) not re-run the source, (2) not re-run done inputs, (3) join over old+new
    path, work = setup(tmp_path, POOL_RESUME.format(timeout=3, work=work_dir(tmp_path)))
    assert run_workflow(path, workdir=work) == 1                # E_DEADLINE
    run = runs_of(path.parent)[0]
    assert read_outcome(run)["error"]["code"] == "E_DEADLINE"
    assert (work / "source-calls").read_text().count("source") == 1
    done_before = {f.name for f in work.glob("body-*")}
    assert {"body-a", "body-b", "body-c"} <= done_before

    (work / "second-pass").touch()                              # input d becomes fast
    assert run_workflow(path, workdir=work, resume_dir=run) == 0
    # source NOT re-executed; a/b/c NOT re-run; d ran exactly once more
    assert (work / "source-calls").read_text().count("source") == 1
    for name in ("a", "b", "c"):
        assert (work / f"body-{name}").read_text().count("run") == 1
    assert (work / "body-d").read_text().count("run") == 2      # first try + resumed
    assert read_outcome(run)["outcome"] == "succeeded"
    manifest = [json.loads(l) for l in
                (run / "steps" / "001-p" / "manifest.jsonl").read_text().splitlines()]
    assert sum(1 for r in manifest if r["ok"]) == 4



def test_decision_resume_continues_at_interrupted_node(tmp_path):
    path, work = setup(tmp_path, DECISION_RESUME.format(work=work_dir(tmp_path)))
    assert run_workflow(path, workdir=work) == 1                # E_DEADLINE at b
    run = runs_of(path.parent)[0]

    (work / "fast").touch()
    assert run_workflow(path, workdir=work, resume_dir=run) == 0
    assert (work / "a-runs").read_text().count("a") == 1        # a NOT re-run
    assert (work / "b-runs").read_text().count("b") == 2        # b re-ran whole (contract)
    journal = [json.loads(l) for l in (run / "journal.jsonl").read_text().splitlines()]
    assert [r["step"] for r in journal] == [1, 2]               # numbering continued, no dupes
    assert journal[0]["message"] == "from-a"                    # last.message survived resume


def test_legacy_pipeline_yaml_still_reads(tmp_path):
    # 4.1 renamed the config to workflow.yaml; untouched pre-4.1 projects
    # (pipeline.yaml) keep working — read side falls back, writes use the
    # new name (the run snapshot is workflow.yaml)
    from medulla.v2.rundir import config_yaml
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    pdir = tmp_path / "legacy"
    pdir.mkdir()
    (pdir / "pipeline.yaml").write_text(text, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    assert run_workflow(config_yaml(pdir), workdir=work) == 0
    run = runs_of(pdir)[0]
    assert (run / "workflow.yaml").is_file()          # snapshot: new name
    # a pre-4.1 run dir (pipeline.yaml snapshot) is still seen by find_resumable
    old_run = pdir / "runs" / "2026-01-01_00-00-00-old1"
    old_run.mkdir(parents=True)
    (old_run / "pipeline.yaml").write_text("x", encoding="utf-8")
    assert find_resumable(pdir) == old_run            # no outcome.json = resumable


def test_crash_window_after_terminal_journal_row_finalizes(tmp_path):
    # the window: terminal row hits journal.jsonl, process dies BEFORE
    # outcome.json — resume must synthesize the outcome, not E_VALIDATION
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>payload</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run = runs_of(path.parent)[0]
    (run / "outcome.json").unlink()                 # simulate the kill window
    assert find_resumable(path.parent) == run       # no outcome = resumable
    assert run_workflow(path, workdir=work, resume_dir=run) == 0
    out = read_outcome(run)
    assert out["outcome"] == "succeeded"
    assert {"steps", "duration_s", "run_id"} <= set(out)

    # same window, failed terminal: exit code and error body survive
    text_fail = text.replace("__exit_ok__", "__exit_fail__")
    (tmp_path / "f").mkdir()
    path2, work2 = setup(tmp_path / "f", text_fail)
    assert run_workflow(path2, workdir=work2) == 2
    run2 = runs_of(path2.parent)[0]
    (run2 / "outcome.json").unlink()
    assert run_workflow(path2, workdir=work2, resume_dir=run2) == 2
    out2 = read_outcome(run2)
    assert out2["outcome"] == "failed"
    assert out2["error"]["message"] == "payload"    # rebuilt from the journal row


def test_journal_records_every_produced_signal(tmp_path):
    # owner decision: the journal row carries EVERY signal the node produced
    # (update/var/bare, stdout order) — not just the one that routed
    text = """
version: "2"
start: a
nodes:
  a:
    shell: |
      echo "<signal:update>halfway</signal:update>"
      echo "<signal:var key=SLUG>x1</signal:var>"
      echo "<signal:ok>done</signal:ok>"
    on_signal: {ok: b}
  b:
    inputs: [only]
    shell: |
      echo "<signal:update>chewing</signal:update>"
      echo "<signal:ready>artifact</signal:ready>"
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run = runs_of(path.parent)[0]
    journal = [json.loads(l) for l in (run / "journal.jsonl").read_text().splitlines()]
    assert [(e["name"], e["message"]) for e in journal[0]["signals"]] == [
        ("update", "halfway"), ("var", "x1"), ("ok", "done")]
    assert journal[0]["signals"][1]["key"] == "SLUG"
    manifest = [json.loads(l) for l in
                (run / "steps" / "002-b" / "manifest.jsonl").read_text().splitlines()]
    assert [e["name"] for e in manifest[0]["signals"]] == ["update", "ready"]


def test_crashed_outcome_shape_normalized(tmp_path):
    # crashed/interrupted outcomes lacked steps/duration_s/run_id (field
    # diff across real runs) — one shape for every outcome.json now
    path, work = setup(tmp_path, POOL_RESUME.format(timeout=3, work=work_dir(tmp_path)))
    assert run_workflow(path, workdir=work) == 1                # E_DEADLINE crash
    out = read_outcome(runs_of(path.parent)[0])
    assert out["outcome"] == "crashed"
    assert {"steps", "duration_s", "run_id", "error"} <= set(out)
    assert out["run_id"] == runs_of(path.parent)[0].name.rsplit("-", 1)[-1]


def test_resume_refuses_finished_run(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run = runs_of(path.parent)[0]
    assert run_workflow(path, workdir=work, resume_dir=run) == 1   # refuse, exit 1
    assert read_outcome(run)["outcome"] == "succeeded"             # outcome untouched


