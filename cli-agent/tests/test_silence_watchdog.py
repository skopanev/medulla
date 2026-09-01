"""Regressions for output watching and the one shared attempt deadline."""
import builtins
import os
import signal
import sys
import threading
import time

from medulla.v2 import procrun


def _run(script, timeout_s, idle, cwd):
    env = {**os.environ, "MEDULLA_IDLE_OUTPUT_S": str(idle)}
    old = os.environ.get("MEDULLA_IDLE_OUTPUT_S")
    os.environ["MEDULLA_IDLE_OUTPUT_S"] = str(idle)
    try:
        import importlib
        importlib.reload(procrun)
        return procrun.run(["bash", "-c", script], cwd=cwd, timeout_s=timeout_s,
                           watch_output=True, extra_env=env)
    finally:
        if old is None:
            os.environ.pop("MEDULLA_IDLE_OUTPUT_S", None)
        else:
            os.environ["MEDULLA_IDLE_OUTPUT_S"] = old
        import importlib
        importlib.reload(procrun)


def test_a_talking_then_silent_agent_is_killed_and_says_why(tmp_path):
    res = _run("echo one; echo two; sleep 30", timeout_s=200, idle=3, cwd=tmp_path)
    assert res.timed_out
    assert res.rc == 124
    assert "silent for 3s" in res.killed_because, res.killed_because
    assert "2 lines" in res.killed_because, "say how much it had produced"


def test_an_agent_that_never_speaks_is_named_differently(tmp_path):
    old = os.environ.get("MEDULLA_FIRST_OUTPUT_S")
    os.environ["MEDULLA_FIRST_OUTPUT_S"] = "2"
    try:
        import importlib
        importlib.reload(procrun)
        res = procrun.run(["bash", "-c", "sleep 30"], cwd=tmp_path, timeout_s=200,
                          watch_output=True)
    finally:
        if old is None:
            os.environ.pop("MEDULLA_FIRST_OUTPUT_S", None)
        else:
            os.environ["MEDULLA_FIRST_OUTPUT_S"] = old
        import importlib
        importlib.reload(procrun)
    assert res.timed_out
    assert "no output at all" in res.killed_because


def test_a_steadily_working_agent_survives(tmp_path):
    res = _run("for i in 1 2 3; do echo tick; sleep 1; done", timeout_s=200,
               idle=3, cwd=tmp_path)
    assert not res.timed_out
    assert res.rc == 0
    assert res.killed_because == ""


def test_an_honest_timeout_carries_no_watchdog_reason(tmp_path):
    res = procrun.run(["bash", "-c", "while :; do echo tick; sleep 0.1; done"],
                      cwd=tmp_path, timeout_s=2, watch_output=False)
    assert res.timed_out and res.rc == 124
    assert res.killed_because == "", "no watchdog ran; the field must stay empty"


def test_watchdog_and_wait_share_one_attempt_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 0.05)
    started = time.monotonic()
    res = procrun.run(
        ["bash", "-c", "while :; do echo tick; sleep 0.05; done"],
        cwd=tmp_path, timeout_s=1, watch_output=True, idle_timeout_s=0.5,
    )
    elapsed = time.monotonic() - started
    assert res.timed_out and res.rc == 124
    assert 0.8 <= elapsed < 1.6, f"one-second attempt lasted {elapsed:.3f}s"


def test_cleanup_grace_is_capped_by_workflow_deadline(tmp_path):
    started = time.monotonic()
    res = procrun.run(
        ["bash", "-c", "trap '' TERM; while :; do echo tick; sleep 0.05; done"],
        cwd=tmp_path, timeout_s=0.5, hard_deadline=started + 0.8,
    )
    elapsed = time.monotonic() - started
    assert res.timed_out and res.rc == 124
    assert 0.7 <= elapsed < 1.2, f"workflow-capped cleanup lasted {elapsed:.3f}s"


def test_timeout_grace_captures_term_handler_signal(tmp_path):
    started = time.monotonic()
    res = procrun.run(
        ["bash", "-c", "trap 'echo \"<signal:ok>saved</signal:ok>\"; exit' TERM; "
         "while :; do sleep 1; done"],
        cwd=tmp_path, timeout_s=0.2,
    )
    elapsed = time.monotonic() - started
    assert res.timed_out and "<signal:ok>saved</signal:ok>" in res.stdout
    assert 0.2 <= elapsed < 1.0, f"TERM handler took {elapsed:.3f}s"


def test_caller_exception_does_not_change_success_cleanup(tmp_path):
    child = (
        "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c',"
        "'import time;time.sleep(10)'],stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL); print(p.pid,flush=True)"
    )
    try:
        raise ValueError("caller state")
    except ValueError:
        res = procrun.run([sys.executable, "-c", child], tmp_path, timeout_s=1)
    pid = int(res.stdout.strip())
    try:
        os.kill(pid, 0)
    finally:
        os.kill(pid, signal.SIGKILL)


def test_capture_is_ready_before_body_result_is_classified(tmp_path, monkeypatch):
    real_read = procrun.OutputCapture._read_pipes

    def delayed_read(capture):
        time.sleep(0.05)
        real_read(capture)

    monkeypatch.setattr(procrun.OutputCapture, "_read_pipes", delayed_read)
    res = procrun.run(
        [sys.executable, "-c", "import sys; sys.stdout.write('<signal:ok>x</signal:ok>')"],
        tmp_path, timeout_s=0.5,
    )
    assert res.rc == 0 and "<signal:ok>x</signal:ok>" in res.stdout


def test_blocked_log_write_cannot_extend_deadline(tmp_path, monkeypatch):
    release = threading.Event()
    log_path = tmp_path / "attempt.log"
    real_open = builtins.open

    class SlowLog:
        def write(self, _text):
            release.wait(2)

        def close(self):
            return None

    monkeypatch.setattr(
        builtins, "open",
        lambda path, *args, **kwargs: SlowLog() if path == log_path
        else real_open(path, *args, **kwargs),
    )
    started = time.monotonic()
    try:
        res = procrun.run(
            ["bash", "-c", "echo blocked; sleep 10"], cwd=tmp_path,
            timeout_s=0.2, log_path=log_path,
        )
    finally:
        release.set()
    elapsed = time.monotonic() - started
    assert res.timed_out and res.rc == 124
    assert elapsed < 0.8, f"blocked log write lasted {elapsed:.3f}s"


def test_background_descendant_cannot_hold_pipe_past_deadline(tmp_path):
    started = time.monotonic()
    res = procrun.run(
        ["bash", "-c", "sleep 3 & echo done"], cwd=tmp_path, timeout_s=0.5,
    )
    elapsed = time.monotonic() - started
    assert res.rc == 0 and not res.timed_out
    assert elapsed < 1.2, f"descendant held the pipe for {elapsed:.3f}s"


def test_signal_cleanup_uses_registered_pgid_and_is_reentrant(monkeypatch):
    class LiveProc:
        returncode = None

        def poll(self):
            return None

    proc = LiveProc()
    calls = []
    with procrun._LIVE_LOCK:
        procrun._LIVE[proc] = 4242
    monkeypatch.setattr(procrun, "_kill_group", lambda *args: calls.append(args))
    try:
        with procrun._LIVE_LOCK:
            procrun.kill_live_processes()
    finally:
        with procrun._LIVE_LOCK:
            procrun._LIVE.pop(proc, None)
    assert calls == [(proc, signal.SIGTERM, 4242)]


def test_failed_reaper_handoff_does_not_leave_live_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(procrun, "_wait_for_exit", lambda *_args: False)
    monkeypatch.setattr(procrun, "_defer_reap", lambda _proc: False)
    res = procrun.run(["bash", "-c", "sleep 10"], cwd=tmp_path, timeout_s=0.1)
    assert res.timed_out and res.rc == 124
    with procrun._LIVE_LOCK:
        assert not procrun._LIVE


def test_explicit_idle_applies_before_first_output(tmp_path):
    started = time.monotonic()
    res = procrun.run(
        ["bash", "-c", "sleep 10"], cwd=tmp_path, timeout_s=3,
        watch_output=True, idle_timeout_s=1,
    )
    elapsed = time.monotonic() - started
    assert res.timed_out and res.rc == 124
    assert "no output at all in 1s" in res.killed_because
    assert elapsed < 1.8, f"explicit idle lasted {elapsed:.3f}s"


def test_env_idle_arms_short_talking_body(tmp_path, monkeypatch):
    monkeypatch.setattr(procrun, "IDLE_OUTPUT_S", 0.5)
    started = time.monotonic()
    res = procrun.run(
        ["bash", "-c", "echo started; sleep 10"], cwd=tmp_path, timeout_s=2,
        watch_output=True,
    )
    elapsed = time.monotonic() - started
    assert res.timed_out and "silent for 0.5s" in res.killed_because
    assert elapsed < 1.3, f"env idle lasted {elapsed:.3f}s"


def test_the_default_threshold_is_no_longer_300():
    assert procrun.IDLE_OUTPUT_S == 900


def test_a_bad_override_falls_back_to_the_default():
    for junk in ("0", "-5", "abc", ""):
        os.environ["MEDULLA_IDLE_OUTPUT_S"] = junk
        try:
            import importlib
            importlib.reload(procrun)
            assert procrun.IDLE_OUTPUT_S == 900, junk
        finally:
            os.environ.pop("MEDULLA_IDLE_OUTPUT_S", None)
            import importlib
            importlib.reload(procrun)
