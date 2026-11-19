"""A stop means a stop: SIGTERM must not buy a new attempt.

Shell bodies only, no model and no agent harness — the defect is in the engine's
cancellation, and a real CLI would only add cost and noise to the proof.
"""
import os
import signal
import threading
import time

import pytest
from conftest import read_manifest, read_run
from conftest import write_workflow as setup
from medulla.v2 import procrun
from medulla.v2.engine import run_workflow
from medulla.v2.procrun import run as proc_run
from medulla.v2.stopflag import clear_stop, request_stop


def _sigterm_once_bodies_run(work, count: int, budget: float = 20.0) -> threading.Thread:
    """SIGTERM this process once `count` bodies have written their attempt id.

    Never a blind timer: the ids prove the handler is installed. A SIGTERM that
    arrives before that runs the DEFAULT disposition and kills the test suite.
    """
    def wait_then_signal():
        marker = work / "attempts.txt"
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if marker.is_file() and len(marker.read_text().splitlines()) >= count:
                time.sleep(0.3)             # both workers inside proc.wait
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(0.05)
    t = threading.Thread(target=wait_then_signal, daemon=True)
    t.start()
    return t


def test_sigterm_in_a_pool_buys_no_new_attempt(tmp_path):
    """The reported shape: a fanout, a stop, and two fresh attempts after it.

    The killed child exits non-zero, the worker thread reads that as an ordinary
    failed attempt, and the retry opens a NEW conversation — while the leader waits
    for it inside ThreadPoolExecutor.shutdown(wait=True). The launcher then sees no
    exit inside its grace and sends SIGKILL.
    """
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [one, two]
    max_parallel: 2
    max_attempts: 3
    shell: 'echo "$MEDULLA_ATTEMPT_ID" >> attempts.txt; sleep 20; exit 1'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    _sigterm_once_bodies_run(work, 2)
    started = time.monotonic()
    rc = run_workflow(path, workdir=work)
    elapsed = time.monotonic() - started

    ids = (work / "attempts.txt").read_text().split()
    assert sorted(ids) == ["001.i1.p1", "001.i2.p1"]   # p2 would be the defect
    assert rc == 130
    run, outcome, _ = read_run(path.parent)
    assert outcome["outcome"] == "interrupted"
    # No row for an input that was interrupted: a resume re-runs it rather than
    # inheriting a failure it never had.
    assert read_manifest(run, "001-p") == []
    # The bodies sleep 20s. One retry means joining another one of those.
    assert elapsed < 10, f"the leader waited {elapsed:.1f}s for work it had cancelled"


def test_a_child_started_after_the_stop_is_refused(tmp_path):
    """The race the flag alone does not close.

    A worker can pass the attempt loop's check a moment before the signal. The
    handler then takes its snapshot of the live children, and the worker registers
    afterwards — so that child is neither killed nor refused, and it outlives the
    run. Registration asks the question under the same lock.
    """
    request_stop()
    try:
        with pytest.raises(KeyboardInterrupt):
            proc_run(["bash", "-c", "sleep 30"], tmp_path, 60)
    finally:
        clear_stop()
    assert not procrun._LIVE


def test_a_new_run_in_this_process_is_not_stopped(tmp_path):
    """The flag is process-wide, so a stopped run must not stop the next one.

    It bites the suite first, then nested runs: a pipeline runs several engines in
    one process.
    """
    request_stop()
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [a, b]
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    try:
        assert run_workflow(path, workdir=work) == 0
    finally:
        clear_stop()
