"""Can the real failure reason be found from the record?

`message` in the journal is a sentence assembled for a reader, and its stderr quote is the
LAST 400 bytes. A lane reporting failures to people asked for the unabridged reason and
could not get it from the journal — reasonably reading the clipping as loss.

It is not loss: the body's full stdout and stderr are on disk, unbounded, written as the
process runs. What was missing is the ADDRESS. These tests pin both halves — that the file
holds everything, and that the journal row says where it is.
"""
import json

import pytest
from conftest import read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow

NOISY = """
version: "2"
start: boom
nodes:
  boom:
    shell: |
      python3 -c "
      import sys
      for i in range(200):
          print(f'TRACE line {i:04d} ' + 'x' * 60, file=sys.stderr)
      print('ROOTCAUSE: the one line somebody needs', file=sys.stderr)
      "
      exit 1
    on_signal: {__failed__: __exit_fail__}
"""


def test_the_file_holds_everything_the_body_printed(tmp_path):
    """No cap on the way to disk: 200 lines of stack plus the cause, all of it."""
    path, work = setup(tmp_path, NOISY)
    assert run_workflow(path, workdir=work) == 2
    run, _outcome, _journal = read_run(path.parent)
    logs = sorted(run.rglob("attempt-*.txt"))
    assert logs, "the attempt log is the durable artifact; without it nothing else matters"
    text = logs[0].read_text()
    assert "ROOTCAUSE: the one line somebody needs" in text
    assert sum(1 for i in range(200) if f"TRACE line {i:04d}" in text) == 200


def test_the_journal_row_says_where_that_file_is(tmp_path):
    """The gap that was actually reported. A reader had to reconstruct
    "steps/001-<node>" from the step number and then guess the filename."""
    path, work = setup(tmp_path, NOISY)
    run_workflow(path, workdir=work)
    run, _outcome, journal = read_run(path.parent)
    row = journal[0]
    assert "log_dir" in row, "a clipped message must carry the address of the full one"
    assert (run / row["log_dir"]).is_dir()
    assert any((run / row["log_dir"]).glob("attempt-*.txt"))


def test_the_clipped_message_keeps_the_END_not_the_start(tmp_path):
    """_tail, deliberately: a failure's last lines are where its cause usually is, and a
    head-clip would hand the reader 400 bytes of stack preamble. The full text is one
    field away, so the quote is a preview and not the record."""
    path, work = setup(tmp_path, NOISY)
    run_workflow(path, workdir=work)
    _run, _outcome, journal = read_run(path.parent)
    message = journal[0]["message"]
    assert "ROOTCAUSE: the one line somebody needs" in message
    assert "TRACE line 0000" not in message


def test_a_pool_seats_own_output_is_addressable_too(tmp_path):
    """A pool keeps one directory per seat under the same step dir, so the address is the
    step and the seat is a subdirectory — nothing special for the reader to learn."""
    text = """
version: "2"
start: fan
nodes:
  fan:
    inputs: [{name: one}, {name: two}]
    min_success: 2
    shell: 'echo "seat {{input.name}} spoke" >&2; echo "<signal:ok>k</signal:ok>"'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _outcome, journal = read_run(path.parent)
    row = next(r for r in journal if r["kind"] == "pool")
    seat_dirs = sorted((run / row["log_dir"]).glob("input-*"))
    assert len(seat_dirs) == 2
    blob = "".join(p.read_text(errors="replace")
                   for d in seat_dirs for p in d.glob("attempt-*.txt"))
    assert "seat one spoke" in blob and "seat two spoke" in blob


def test_a_hooks_output_is_in_the_same_place(tmp_path):
    """pre and post write beside the attempts. A veto's real reason is in post-N.txt,
    whatever the 400-byte quote in the message shows."""
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo body'
    post: |
      python3 -c "
      import sys
      for i in range(100): print(f'HOOK detail {i:03d}', file=sys.stderr)
      "
      exit 1
    on_signal: {__failed__: __exit_fail__}
"""
    path, work = setup(tmp_path, text)
    run_workflow(path, workdir=work)
    run, _outcome, journal = read_run(path.parent)
    post = sorted((run / journal[0]["log_dir"]).glob("post-*.txt"))
    assert post, "the hook's own output must be on disk, not only quoted"
    assert "HOOK detail 000" in post[0].read_text()


def test_the_attempt_log_path_reaches_a_post_hook(tmp_path):
    """MEDULLA_ATTEMPT_LOG: a hook that wants to say WHY reads the body's full output
    rather than being handed a quote. Already contract; pinned so it stays one."""
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "the whole truth" >&2; exit 1'
    post: 'test -s "$MEDULLA_ATTEMPT_LOG" && grep -q "the whole truth" "$MEDULLA_ATTEMPT_LOG" && echo seen > seen.txt'
    on_signal: {__failed__: __exit_fail__}
"""
    path, work = setup(tmp_path, text)
    run_workflow(path, workdir=work)
    assert (work / "seen.txt").exists(), "the hook could not read the body's full output"
