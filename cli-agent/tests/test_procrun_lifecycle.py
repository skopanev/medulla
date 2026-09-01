"""Process, pipe, sink, and signal cleanup must stay inside bounded ownership."""
import builtins
import os
import signal
import sys
import threading
import time

import pytest
from medulla.v2 import procrun


def _escaped_child_script() -> str:
    return (
        "import subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'], "
        "stdout=sys.stdout, stderr=sys.stderr, start_new_session=True); "
        "print(p.pid, flush=True)"
    )


def test_successful_body_uses_one_drain_cap_for_both_pipes(tmp_path, monkeypatch):
    monkeypatch.setattr(procrun, "PIPE_DRAIN_S", 0.2)
    started = time.monotonic()
    res = procrun.run(
        [sys.executable, "-c", _escaped_child_script()],
        cwd=tmp_path, timeout_s=3,
    )
    elapsed = time.monotonic() - started
    escaped_pid = int(res.stdout.strip())
    try:
        assert res.rc == 0 and not res.timed_out
        assert elapsed < 0.4, f"two pipes exceeded one drain cap: {elapsed:.3f}s"
    finally:
        os.kill(escaped_pid, signal.SIGKILL)


def test_interrupt_inside_watcher_reaches_cleanup(tmp_path, monkeypatch):
    calls = []
    real_kill = procrun._kill_group

    def interrupt(*_args):
        time.sleep(0.1)
        raise KeyboardInterrupt

    def track_kill(*args):
        calls.append(args[1])
        real_kill(*args)

    monkeypatch.setattr(procrun, "_watch_output", interrupt)
    monkeypatch.setattr(procrun, "_kill_group", track_kill)
    with pytest.raises(KeyboardInterrupt):
        procrun.run(
            ["bash", "-c", "trap '' TERM; while :; do sleep 1; done"],
            cwd=tmp_path, timeout_s=0.3, watch_output=True, idle_timeout_s=0.1,
        )
    assert signal.SIGTERM in calls
    with procrun._LIVE_LOCK:
        assert not procrun._LIVE


def test_interrupt_inside_capture_finish_releases_ownership(tmp_path, monkeypatch):
    captures = []
    real_capture = procrun.OutputCapture
    real_join = threading.Thread.join
    interrupted = False

    def capture(*args):
        instance = real_capture(*args)
        captures.append(instance)
        return instance

    def interrupt_join(thread, *args, **kwargs):
        nonlocal interrupted
        if thread.name == "procrun-capture" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return real_join(thread, *args, **kwargs)

    monkeypatch.setattr(procrun, "OutputCapture", capture)
    monkeypatch.setattr(threading.Thread, "join", interrupt_join)
    with pytest.raises(KeyboardInterrupt):
        procrun.run(["bash", "-c", "echo done"], cwd=tmp_path, timeout_s=1)
    assert interrupted and not captures[0]._capture.is_alive()
    assert all(pipe.closed for pipe, _buf, _tag in captures[0]._pipes)
    with procrun._LIVE_LOCK:
        assert not procrun._LIVE


def test_capture_constructor_interrupt_cleans_child(tmp_path, monkeypatch):
    calls = []
    real_kill = procrun._kill_group

    def interrupt_ctor(*_args):
        raise KeyboardInterrupt

    def track_kill(*args):
        calls.append(args[1])
        real_kill(*args)
        if len(calls) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(procrun, "OutputCapture", interrupt_ctor)
    monkeypatch.setattr(procrun, "_kill_group", track_kill)
    with pytest.raises(KeyboardInterrupt):
        procrun.run(["bash", "-c", "sleep 10"], cwd=tmp_path, timeout_s=1)
    assert signal.SIGTERM in calls and signal.SIGKILL in calls
    with procrun._LIVE_LOCK:
        assert not procrun._LIVE


def test_capture_start_interrupt_closes_partial_resources(tmp_path, monkeypatch):
    closed = threading.Event()
    captures = []
    real_open = builtins.open
    real_capture = procrun.OutputCapture
    real_start = threading.Thread.start
    log_path = tmp_path / "attempt.log"

    class TrackingLog:
        def close(self):
            closed.set()

    def capture(*args):
        instance = real_capture(*args)
        captures.append(instance)
        return instance

    def interrupt_start(thread):
        if thread.name == "procrun-capture":
            raise KeyboardInterrupt
        return real_start(thread)

    monkeypatch.setattr(
        builtins, "open",
        lambda path, *args, **kwargs: TrackingLog() if path == log_path
        else real_open(path, *args, **kwargs),
    )
    monkeypatch.setattr(procrun, "OutputCapture", capture)
    monkeypatch.setattr(threading.Thread, "start", interrupt_start)
    with pytest.raises(KeyboardInterrupt):
        procrun.run(
            ["bash", "-c", "sleep 10"], cwd=tmp_path,
            timeout_s=1, log_path=log_path,
        )
    assert closed.wait(0.2)
    assert all(pipe.closed for pipe, _buf, _tag in captures[0]._pipes)
    with procrun._LIVE_LOCK:
        assert not procrun._LIVE


def test_blocked_echo_does_not_delay_success(tmp_path):
    release = threading.Event()

    def echo(_tag, _line):
        release.wait(2)

    started = time.monotonic()
    try:
        res = procrun.run(
            ["bash", "-c", "echo done"], cwd=tmp_path,
            timeout_s=3, echo=echo,
        )
    finally:
        release.set()
    elapsed = time.monotonic() - started
    assert res.rc == 0 and res.stdout == "done\n"
    assert elapsed < 0.3, f"best-effort echo delayed success {elapsed:.3f}s"


def test_blocked_log_cannot_hide_routing_signal(tmp_path, monkeypatch):
    release = threading.Event()
    log_path = tmp_path / "attempt.log"
    real_open = builtins.open

    class SlowLog:
        def write(self, _text):
            release.wait(2)

        def close(self):
            return None

    monkeypatch.setattr(procrun, "PIPE_DRAIN_S", 0.2)
    monkeypatch.setattr(
        builtins, "open",
        lambda path, *args, **kwargs: SlowLog() if path == log_path
        else real_open(path, *args, **kwargs),
    )
    child = (
        _escaped_child_script()
        + "; sys.stdout.write('<signal:ok>done</signal:ok>'); sys.stdout.flush()"
    )
    try:
        res = procrun.run(
            [sys.executable, "-c", child], cwd=tmp_path,
            timeout_s=3, log_path=log_path,
        )
        escaped_pid = int(res.stdout.splitlines()[0])
        assert res.rc == 0 and not res.timed_out
        assert "<signal:ok>done</signal:ok>" in res.stdout
    finally:
        release.set()
        if "escaped_pid" in locals():
            os.kill(escaped_pid, signal.SIGKILL)


def test_escaped_pipe_holder_does_not_retain_log(tmp_path, monkeypatch):
    closed = threading.Event()
    log_path = tmp_path / "attempt.log"
    real_open = builtins.open

    class TrackingLog:
        def write(self, _text):
            return None

        def close(self):
            closed.set()

    monkeypatch.setattr(procrun, "PIPE_DRAIN_S", 0.2)
    monkeypatch.setattr(
        builtins, "open",
        lambda path, *args, **kwargs: TrackingLog() if path == log_path
        else real_open(path, *args, **kwargs),
    )
    captures = []
    real_capture = procrun.OutputCapture

    def capture(*args):
        instance = real_capture(*args)
        captures.append(instance)
        return instance

    monkeypatch.setattr(procrun, "OutputCapture", capture)
    res = procrun.run(
        [sys.executable, "-c", _escaped_child_script()],
        cwd=tmp_path, timeout_s=3, log_path=log_path,
    )
    escaped_pid = int(res.stdout.strip())
    try:
        assert closed.wait(0.2), "sink retained the attempt log"
        assert not captures[0]._capture.is_alive()
        assert all(pipe.closed for pipe, _buf, _tag in captures[0]._pipes)
    finally:
        os.kill(escaped_pid, signal.SIGKILL)
