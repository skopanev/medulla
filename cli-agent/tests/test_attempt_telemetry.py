"""Timings per attempt, so the next threshold is argued from data.

Every watchdog number this project ever used — 300, 900, 1800 — was set from
exactly one incident, and 1800 came from a run that was read wrong. Nothing
accumulated the timings that would let the next number be chosen differently.
Until the journal can answer "what is the real maximum gap in output, by harness
and model", no threshold should be touched.
"""
import json

from conftest import fake_script, read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow


def attempts(run):
    path = run / "attempts.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_a_finished_attempt_records_its_timings(tmp_path):
    script = fake_script(tmp_path, "chatty.sh",
                         "echo start; sleep 0.4; echo '<signal:done>ok</signal:done>'\n")
    text = f"""
version: "2"
start: n
nodes:
  n:
    agent: {{harness: fake, model: {script}}}
    prompt: work
    on_signal: {{done: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    rows = attempts(run)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["harness"] == "fake"
    assert row["rc"] == 0 and row["timed_out"] is False
    assert row["read_events"] >= 2, row
    # The number the thresholds have been guessing at, measured instead.
    assert 0.3 < row["max_gap_s"] < 2.0, row["max_gap_s"]
    assert row["first_byte_s"] is not None
    assert row["last_byte_s"] >= row["first_byte_s"]
    assert row["duration_s"] >= row["last_byte_s"]


def test_a_retried_attempt_is_not_erased_by_the_one_that_succeeded(tmp_path):
    """The journal keeps one row per completed STEP, so a retry that eventually
    worked left no trace of the attempt that failed — calibration then saw only
    final failures and kept confirming the heuristic."""
    counter = tmp_path / "n.txt"
    script = fake_script(tmp_path, "flaky.sh",
                         f"n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); "
                         f"echo $n > {counter}; "
                         f"if [ $n -lt 2 ]; then echo nope; exit 3; fi; "
                         f"echo '<signal:done>ok</signal:done>'\n")
    text = f"""
version: "2"
start: n
nodes:
  n:
    agent: {{harness: fake, model: {script}}}
    max_attempts: 3
    prompt: work
    on_signal: {{done: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    rows = attempts(run)
    assert len(rows) == 2, f"both attempts must survive: {rows}"
    assert rows[0]["rc"] == 3 and rows[0]["reason"] == "rc"
    assert rows[1]["rc"] == 0


def test_the_journal_answers_the_question_it_was_built_for(tmp_path):
    """Acceptance, literally: a number out of the journal, grouped by harness."""
    script = fake_script(tmp_path, "paced.sh",
                         "echo a; sleep 0.3; echo b; echo '<signal:done>ok</signal:done>'\n")
    text = f"""
version: "2"
start: n
nodes:
  n:
    agent: {{harness: fake, model: {script}}}
    prompt: work
    on_signal: {{done: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    run_workflow(path, workdir=work)
    run, _, _ = read_run(path.parent)
    finished = [r for r in attempts(run) if not r["timed_out"]]
    by_harness = {}
    for row in finished:
        by_harness.setdefault(row["harness"], []).append(row["max_gap_s"])
    worst = {h: max(gaps) for h, gaps in by_harness.items()}
    assert worst and all(isinstance(v, float) for v in worst.values()), worst
    assert worst["fake"] > 0.2, worst
