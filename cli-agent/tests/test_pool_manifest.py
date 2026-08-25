"""Pools: the manifest row is the record of what each input did.

It is what the join reads, what a resume replays, and what a later node consumes — so
what lands in it matters more than what was printed.
"""
import json

from medulla.v2.engine import run_workflow

from conftest import fake_script, read_manifest, read_run, write_workflow as setup

def test_manifest_env_and_key_stability(tmp_path):
    text = """
version: "2"
start: fix-all
nodes:
  fix-all:
    inputs: [{id: 7}]
    shell: "true"
    on_signal: {__done__: consume}
  consume:
    shell: |
      jq -s -r '.[0].key' "$MEDULLA_MANIFEST_FIX_ALL" > key.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    key = (work / "key.txt").read_text().strip()
    assert key.startswith("1:") and len(key.split(":")[1]) == 16   # (index, sha256[:16])


def test_pool_signal_never_rescues_dead_input(tmp_path):
    # conjunction law: "echo <signal:partial>; exit 7" must NOT be ok — in pools
    # signals are data, never a verdict
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [x]
    shell: 'echo "<signal:partial>said something</signal:partial>"; exit 7'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    run, _, _ = read_run(path.parent)
    row = read_manifest(run, "001-p")[0]
    assert row["ok"] is False and row["reason"] == "rc" and row["rc"] == 7


def test_pool_records_signal_as_data_on_ok(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [x]
    shell: 'echo "<signal:insight>found the bug</signal:insight>"'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    row = read_manifest(run, "001-p")[0]
    assert row["ok"] is True and row["signal"] == "insight"
    assert row["message"] == "found the bug"


def test_defaults_ignore_exit_code_never_reaches_pools(tmp_path):
    text = """
version: "2"
start: p
defaults: {ignore_exit_code: true}
nodes:
  p:
    inputs: [x]
    shell: "exit 7"
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2       # min_success owns this role
    run, _, _ = read_run(path.parent)
    assert read_manifest(run, "001-p")[0]["ok"] is False


def test_parallel_pre_vars_blacklist_enforced(tmp_path):
    # a parallel pre emitting PATH must not poison the worker env (or crash the run)
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [x, y]
    max_parallel: 2
    pre: 'echo "<signal:var key=PATH>/nope</signal:var>"'
    shell: 'echo ok >> "done-$MEDULLA_INPUT_INDEX"'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert (work / "done-1").exists() and (work / "done-2").exists()


def test_source_arrays_rejected(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: {shell: "printf '[[1,2]]'"}
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_INPUTS"


def test_source_jsonl_inputs(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: {shell: "printf '{\\"id\\": 1}\\n{\\"id\\": 2}\\n'"}
    shell: 'echo "$MEDULLA_INPUT_ID" >> ids.txt'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert sorted((work / "ids.txt").read_text().split()) == ["1", "2"]


def test_deadline_killed_input_has_no_row(tmp_path):
    # a single slow input killed by the RUN budget (not its own timeout) must leave
    # no manifest row — a reason:timeout row would never be retried on resume
    text = """
version: "2"
start: p
timeout: 1
nodes:
  p:
    inputs: [slow]
    shell: "sleep 30"
    timeout: 300
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1
    run, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_DEADLINE"
    assert read_manifest(run, "001-p") == []


def test_parallel_deadline_path(tmp_path):
    text = """
version: "2"
start: p
timeout: 2
nodes:
  p:
    inputs: [f1, f2, f3, slow]
    max_parallel: 4
    shell: '[ "$MEDULLA_INPUT" = slow ] && sleep 30 || true'
    timeout: 300
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1
    run, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_DEADLINE"
    rows = read_manifest(run, "001-p")
    assert {r["input"] for r in rows} == {"f1", "f2", "f3"}   # fast rows survive, slow has none


def test_parallel_pre_vars_isolated_between_workers(tmp_path):
    # each worker's pre var must be visible to ITS body only
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [x, y]
    max_parallel: 2
    pre: 'echo "<signal:var key=MINE>$MEDULLA_INPUT</signal:var>"'
    shell: '[ "$MINE" = "$MEDULLA_INPUT" ] || exit 9'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0


def test_deadline_mid_pool_preserves_manifest_rows(tmp_path):
    text = """
version: "2"
start: p
timeout: 2
nodes:
  p:
    inputs: [fast1, fast2, slow]
    shell: '[ "$MEDULLA_INPUT" = slow ] && sleep 30 || true'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 1
    run, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_DEADLINE"
    rows = read_manifest(run, "001-p")
    done = {r["input"] for r in rows}
    assert {"fast1", "fast2"} <= done                   # concluded rows survive
    assert all(r["input"] != "slow" or not r["ok"] for r in rows)
