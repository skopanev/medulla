"""Pools: the join, min_success, and what silence means.

A pool body that says nothing is OK — that is the contract for shell fan-out — so the
threshold, not the noise, decides whether the node succeeded.
"""
import json

from conftest import fake_script, read_manifest, read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow

# ── basics: silence is ok, manifest rows, join ──────────────────────────────

def test_pool_silent_shell_inputs_are_ok(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [alpha, beta, gamma]
    max_parallel: 3
    shell: 'echo "processed $MEDULLA_INPUT" >> done.txt'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, journal = read_run(path.parent)
    rows = read_manifest(run, "001-p")
    assert len(rows) == 3 and all(r["ok"] and r["reason"] == "ok" for r in rows)
    assert sorted(r["input"] for r in rows) == ["alpha", "beta", "gamma"]
    assert journal[0]["kind"] == "pool" and journal[0]["inputs_ok"] == 3
    assert len((work / "done.txt").read_text().splitlines()) == 3


def test_pool_silence_never_burns_attempts(tmp_path):
    # pool_mode: an agent body that writes artifacts but emits no signal is OK
    script = fake_script(tmp_path, "worker.sh", 'echo run >> "invocations-$MEDULLA_INPUT_INDEX"\nexit 0\n')
    text = f"""
version: "2"
start: p
nodes:
  p:
    inputs: [x]
    agent: {{harness: fake, model: "{script}"}}
    prompt: "p"
    max_attempts: 3
    on_signal: {{__done__: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert (work / "invocations-1").read_text().count("run") == 1   # no silence retries
    run, _, _ = read_run(path.parent)
    assert read_manifest(run, "001-p")[0]["attempts"] == 1


def test_min_success_threshold_done_and_failed(tmp_path):
    body = 'case "$MEDULLA_INPUT" in ok*) exit 0;; *) exit 1;; esac'
    for ms, expected_exit, expected_signal in ((2, 0, "__done__"), (3, 2, "__failed__")):
        base = tmp_path / f"ms{ms}"
        base.mkdir()
        text = f"""
version: "2"
start: p
nodes:
  p:
    inputs: [ok1, ok2, bad]
    max_parallel: 3
    min_success: {ms}
    shell: '{body}'
    on_signal: {{__done__: __exit_ok__}}
"""
        path, work = setup(base, text)
        assert run_workflow(path, workdir=work) == expected_exit
        _, outcome, journal = read_run(path.parent)
        assert journal[0]["signal"] == expected_signal
        if expected_exit == 2:
            assert "2/3 inputs ok" in outcome["error"]["message"]
            assert "rc x1" in outcome["error"]["message"]


def test_no_short_circuit_all_inputs_run(tmp_path):
    # min_success: 1 satisfied by the first input — the rest must still run
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [a, b, c, d]
    min_success: 1
    shell: 'echo "$MEDULLA_INPUT" >> ran.txt'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert len((work / "ran.txt").read_text().splitlines()) == 4


def test_min_success_above_total_is_failed_not_crash(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [only-one]
    min_success: 5
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2


