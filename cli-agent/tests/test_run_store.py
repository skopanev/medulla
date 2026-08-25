"""The run store: locks, selection, and pruning.

Which run --resume picks, why a second process cannot take one that is live, and what
keep_runs removes.
"""
import fcntl
import json
import os
from pathlib import Path

from conftest import POOL_RESUME, runs_of, work_dir
from conftest import write_workflow as setup
from medulla.v2.cli import main as cli_main
from medulla.v2.engine import find_resumable, run_workflow


def test_find_resumable_selection(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "sleep 30"
    timeout: 300
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text.replace('"sleep 30"', '"exit 1"'))
    assert run_workflow(path, workdir=work) == 2                # failed -> NOT resumable
    assert find_resumable(path.parent) is None

    # a crashed (E_DEADLINE-class) run IS resumable (documented deviation)
    (path.parent / "runs" / "2026-01-01_00-00-00-aaaa").mkdir(parents=True)
    crashed = path.parent / "runs" / "2026-01-01_00-00-00-aaaa"
    (crashed / "workflow.yaml").write_text("x", encoding="utf-8")
    (crashed / "outcome.json").write_text(
        json.dumps({"outcome": "crashed", "error": {"code": "E_DEADLINE"}}), encoding="utf-8")
    assert find_resumable(path.parent) == crashed


def test_flock_blocks_second_process(tmp_path):
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
    (run / "outcome.json").unlink()                             # make it resumable
    # simulate a live holder
    fd = os.open(run / ".lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert run_workflow(path, workdir=work, resume_dir=run) == 1
    finally:
        os.close(fd)


def test_truncated_manifest_tail_tolerated(tmp_path):
    path, work = setup(tmp_path, POOL_RESUME.format(timeout=3, work=work_dir(tmp_path)))
    assert run_workflow(path, workdir=work) == 1
    run = runs_of(path.parent)[0]
    manifest = run / "steps" / "001-p" / "manifest.jsonl"
    with open(manifest, "a", encoding="utf-8") as f:
        f.write('{"index": 99, "key": "99:torn')                # crash-torn tail
    (work / "second-pass").touch()
    assert run_workflow(path, workdir=work, resume_dir=run) == 0   # tail dropped, not fatal


def test_prune_keeps_newest_and_active(tmp_path):
    text = """
version: "2"
start: a
keep_runs: 3
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    runs = path.parent / "runs"
    runs.mkdir(exist_ok=True)
    for i in range(6):                                          # old finished runs
        d = runs / f"2026-01-0{i + 1}_00-00-00-old{i}"
        d.mkdir(parents=True)
        (d / "workflow.yaml").write_text("x", encoding="utf-8")
        (d / "outcome.json").write_text('{"outcome": "succeeded"}', encoding="utf-8")
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    young = runs / f"{ts}-live"                                 # unfinished + young: shielded
    young.mkdir()
    (young / "workflow.yaml").write_text("x", encoding="utf-8")

    assert run_workflow(path, workdir=work) == 0
    names = {p.name for p in runs.iterdir()}
    assert young.name in names                                  # active shield held
    # prune runs at BOOT (the new run isn't finished yet): 6 finished -> keep 3 newest
    assert sorted(n for n in names if "old" in n) == [
        "2026-01-04_00-00-00-old3", "2026-01-05_00-00-00-old4", "2026-01-06_00-00-00-old5"]


