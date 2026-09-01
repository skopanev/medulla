"""v2 subprocess runner. Contract differences from v1 run_command (panel-mandated):

- extra_env parameter; os.environ is NEVER mutated
- no signal callback, no kill-on-first-signal: the full body output is captured so
  post hooks and signal-vs-rc precedence can work; signals are extracted post-hoc
- stdout and stderr stream to the attempt log as they arrive (tail -f friendly)
- timeout -> rc 124 (contract: timeout is recognizable as rc 124)
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .model import TIMEOUT_RC
from .procrun_io import STOP_GRACE_S, OutputCapture
from .procrun_io import defer_reap as _defer_reap
from .procrun_io import watch_output as _watch_output

# every live child, registered for signal-time group-kill: an interrupt must
# reach POOL WORKERS' children too (the exception only lands in the main
# thread; workers sit in proc.wait until their agents die)
_LIVE: dict[subprocess.Popen, int] = {}
_LIVE_LOCK = threading.RLock()


def kill_live_processes() -> None:
    """Signal-handler duty: SIGTERM every registered child's process group so
    worker threads unblock immediately and the engine can conclude."""
    with _LIVE_LOCK:
        procs = []
        for proc, pgid in list(_LIVE.items()):
            if proc.returncode is None:
                procs.append((proc, pgid))
            else:
                _LIVE.pop(proc, None)
    for proc, pgid in procs:
        _kill_group(proc, signal.SIGTERM, pgid)


@dataclass
class RunResult:
    rc: int
    timed_out: bool
    stdout: str
    stderr: str
    # WHY it was killed, when the watchdog did it. Empty for an honest timeout.
    # Without this a watchdog kill and a node timeout are the same rc=124 in the
    # manifest, and telling them apart cost an hour on a live P0.
    killed_because: str = ""


def _env_seconds(name: str, default: int) -> int:
    """Read a positive-seconds machine fallback; invalid values use the default."""
    raw = os.environ.get(name, "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return default
    return int(raw)


FIRST_OUTPUT_S = _env_seconds("MEDULLA_FIRST_OUTPUT_S", 60)
# And then it keeps talking. Measured on a healthy opencode round: 298 events in 586
# seconds, median gap 0s, 90th percentile 3s, longest 66s — no pause over two minutes
# in the whole run. Five minutes is generous by a factor of four and still catches a
# body that died mid-work, which is what a live panel did: three panelists went quiet
# for 10-14 minutes each and burned a 1800s timeout, then a retry burned another.
# 300 was calibrated against that healthy round and proved WRONG for real work: a
# panel of four models was killed mid-generation, each after ~279s of output and
# exactly 300s of thought. A model deliberating on a hard review is not a dead one.
# 900 still catches the 10-14 minute silences this exists for.
IDLE_OUTPUT_S = _env_seconds("MEDULLA_IDLE_OUTPUT_S", 900)  # agent field overrides
PIPE_DRAIN_S = 60
CLEANUP_GRACE_S = 3


def run(
    command: str | list[str],
    cwd: Path,
    timeout_s: float,
    extra_env: dict[str, str] | None = None,
    log_path: Path | None = None,
    stdin_data: str | None = None,
    env_remove: list[str] | None = None,
    merge_stderr: bool = False,
    echo=None,   # callable(tag, line) for live operator streaming
    watch_output: bool = False,   # only for agent CLIs — see _watch_output
    idle_timeout_s: float | None = None,  # declared agent value; None -> env/default
    hard_deadline: float | None = None,  # workflow cap includes cleanup grace
) -> RunResult:
    deadline = hard_deadline if hard_deadline is not None else float("inf")
    cleanup_deadline = deadline
    if isinstance(command, str):
        # bash, not $SHELL — same reason as engine.py: hooks are workflow code and must not
        # change meaning because the operator's login shell differs (zsh does no word
        # splitting on `$var`). MEDULLA_SHELL overrides for a deliberate choice.
        shell = os.environ.get("MEDULLA_SHELL", "bash")
        argv = [shell, "-lc", command]
    else:
        argv = command

    env = {**os.environ, **(extra_env or {})}
    for key in env_remove or ():
        env.pop(key, None)
    # "w": a retried/resumed attempt reusing this path must not stack stale
    # layers under the fresh output (audit R4)
    log_file = open(log_path, "w", encoding="utf-8", buffering=1) if log_path else None
    proc = None
    pgid = None
    capture = None
    registered = False
    timed_out = False
    reaper_started = False
    exceptional = True
    went_quiet = ""
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd),
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True, env=env,
            errors="replace",
        )
        deadline = time.monotonic() + max(timeout_s, 0)
        if hard_deadline is not None:
            deadline = min(deadline, hard_deadline)
        cleanup_deadline = deadline
        pgid = proc.pid  # start_new_session makes it the process-group leader
        registered = True
        with _LIVE_LOCK:
            _LIVE[proc] = pgid
        capture = OutputCapture(proc, log_file, echo)
        if not capture.start(deadline):
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout_s)
            raise RuntimeError("unable to start subprocess output capture")
        if stdin_data is not None:
            # A child that never reads stdin must not deadlock us.
            def _feed():
                try:
                    proc.stdin.write(stdin_data)
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass
            threading.Thread(target=_feed, daemon=True).start()
        idle = IDLE_OUTPUT_S if idle_timeout_s is None else idle_timeout_s
        if watch_output and (timeout_s > FIRST_OUTPUT_S * 2 or timeout_s > idle):
            first_output = min(FIRST_OUTPUT_S, idle) if idle_timeout_s is not None \
                else FIRST_OUTPUT_S
            went_quiet = _watch_output(
                proc, capture.out_buf, capture.err_buf, deadline, idle, first_output,
            )
        if went_quiet:
            raise subprocess.TimeoutExpired(argv, timeout_s)
        if not _wait_for_exit(proc, deadline - time.monotonic()):
            raise subprocess.TimeoutExpired(argv, timeout_s)
        exceptional = False
    except subprocess.TimeoutExpired:
        timed_out = True
        cleanup_deadline = time.monotonic() + CLEANUP_GRACE_S
        if hard_deadline is not None:
            cleanup_deadline = min(cleanup_deadline, hard_deadline)
        termination_deadline = max(
            time.monotonic(), cleanup_deadline - STOP_GRACE_S,
        )
        _kill_group(proc, signal.SIGTERM, pgid)
        remaining = max(0, termination_deadline - time.monotonic())
        if remaining == 0 or not _wait_for_exit(proc, min(3, remaining)):
            _kill_group(proc, signal.SIGKILL, pgid)
            remaining = max(0, termination_deadline - time.monotonic())
            if not _wait_for_exit(proc, min(3, remaining)):
                reaper_started = _defer_reap(proc)
        exceptional = False
    except BaseException:
        exceptional = True
        if proc is None:
            if log_file:
                log_file.close()
            raise
        # KeyboardInterrupt or anything else: the child MUST NOT outlive us
        _kill_group(proc, signal.SIGTERM, pgid)
        remaining = max(0, deadline - time.monotonic())
        if remaining == 0 or not _wait_for_exit(proc, min(2, remaining)):
            _kill_group(proc, signal.SIGKILL, pgid)
            remaining = max(0, deadline - time.monotonic())
            if not _wait_for_exit(proc, min(2, remaining)):
                reaper_started = _defer_reap(proc)
        raise
    finally:
        try:
            if proc is not None:
                if exceptional and not reaper_started:
                    _kill_group(proc, signal.SIGKILL, pgid)
                drain_limit = cleanup_deadline if timed_out else min(
                    time.monotonic() + 2 * STOP_GRACE_S,
                    hard_deadline if hard_deadline is not None else float("inf"),
                )
                drain_deadline = time.monotonic() if exceptional else min(
                    drain_limit, time.monotonic() + PIPE_DRAIN_S,
                )
                pumps_alive = capture.finish(drain_deadline) if capture else False
                if capture is None:
                    for pipe in (proc.stdout, proc.stderr):
                        if pipe:
                            pipe.close()
                    if log_file:
                        log_file.close()
                if not reaper_started and (pumps_alive or proc.poll() is None):
                    _kill_group(proc, signal.SIGKILL, pgid)
        finally:
            if registered:
                with _LIVE_LOCK:
                    _LIVE.pop(proc, None)

    rc = TIMEOUT_RC if timed_out else proc.returncode
    return RunResult(rc=rc, timed_out=timed_out,
                     stdout="".join(list(capture.out_buf)),
                     stderr="".join(list(capture.err_buf)),
                     killed_because=went_quiet)


def _kill_group(proc: subprocess.Popen, sig, pgid: int | None = None) -> None:
    try:
        os.killpg(pgid if pgid is not None else os.getpgid(proc.pid), sig)
    except Exception:
        try:
            proc.send_signal(sig)
        except Exception:
            pass


def _wait_for_exit(proc: subprocess.Popen, timeout: float) -> bool:
    if timeout <= 0:
        return proc.poll() is not None
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
