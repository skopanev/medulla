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
    # Timing facts, seconds relative to this attempt's start (None = never
    # happened). Recorded so a threshold can one day be chosen from data rather
    # than from the single incident that prompted the last one: every watchdog
    # number in this project's history — 300, 900, 1800 — came from exactly one
    # run, because nothing accumulated the timings to argue with.
    duration_s: float = 0.0
    first_byte_s: float | None = None
    last_byte_s: float | None = None
    read_events: int = 0
    max_gap_s: float = 0.0


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
CLEANUP_GRACE_S = 3
# How long a Ctrl-C may take: TERM, then this long, then KILL, then this long
# again before the child is handed to the reaper. Named because "how long does
# stopping take" was previously answerable only by reading two bare 2s in the
# interrupt path — and an unnamed budget is one nobody can hold you to.
INTERRUPT_GRACE_S = 2


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
    started_at = time.monotonic()
    log_file = open(log_path, "w", encoding="utf-8", buffering=1) if log_path else None
    proc = None
    pgid = None
    capture = None
    feeder = None
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
            feeder = threading.Thread(target=_feed, daemon=True)
            feeder.start()
        idle = IDLE_OUTPUT_S if idle_timeout_s is None else idle_timeout_s
        if watch_output and (timeout_s > FIRST_OUTPUT_S * 2 or timeout_s > idle):
            # One value, two ways to set it, one behaviour: the declared
            # idle_timeout used to be clamped here and the env one was not, so
            # MEDULLA_IDLE_OUTPUT_S=10 kept a 60s first-output window while
            # `idle_timeout: 10` on the node did not. A first-output grace longer
            # than the idle threshold outlives the threshold it belongs to.
            first_output = min(FIRST_OUTPUT_S, idle)
            went_quiet = _watch_output(
                proc, capture, deadline, idle, first_output,
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
        if remaining == 0 or not _wait_for_exit(proc, min(INTERRUPT_GRACE_S, remaining)):
            _kill_group(proc, signal.SIGKILL, pgid)
            remaining = max(0, deadline - time.monotonic())
            if not _wait_for_exit(proc, min(INTERRUPT_GRACE_S, remaining)):
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
                drain_deadline = time.monotonic() if exceptional else drain_limit
                pumps_alive = True     # unknown until finish returns: assume alive
                try:
                    pumps_alive = capture.finish(drain_deadline) if capture else False
                finally:
                    # The terminal group KILL happens even if an interrupt lands
                    # INSIDE finish. It used to sit at the end of this block, so a
                    # Ctrl-C in that window skipped it and the child outlived the
                    # stop — the very thing the docker layer prevents from outside
                    # and the engine must guarantee from inside.
                    if not reaper_started and (pumps_alive or proc.poll() is None):
                        _kill_group(proc, signal.SIGKILL, pgid)
                if proc.stdin is not None:
                    # stdin is closed HERE, always, and the feed thread is joined.
                    # Two leaks met at this line. If the capture failed before the
                    # feed thread started, the descriptor had no owner at all and
                    # max_attempts repeated the attempt, accumulating exactly when
                    # the system was already in a bad state. And a detached child
                    # inheriting stdin kept a blocking daemon writer alive AFTER a
                    # SUCCESSFUL run — holding the payload (up to 8 MiB), the
                    # thread and the descriptor, outside any budget: neither the
                    # node timeout nor the workflow deadline can see it. A panel is
                    # five bodies with a prompt each, retried; that is a dozen held
                    # buffers per round, and the manifest says nothing.
                    # Closing wakes a blocked write as a broken pipe, which _feed
                    # already swallows.
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass
                    if feeder is not None:
                        feeder.join(timeout=STOP_GRACE_S)
                if capture is None:
                    for pipe in (proc.stdout, proc.stderr):
                        if pipe:
                            pipe.close()
                    if log_file:
                        log_file.close()
        finally:
            if registered:
                with _LIVE_LOCK:
                    _LIVE.pop(proc, None)

    rc = TIMEOUT_RC if timed_out else proc.returncode
    rel = (lambda t: None if t is None else round(t - started_at, 3))
    return RunResult(rc=rc, timed_out=timed_out,
                     stdout="".join(list(capture.out_buf)),
                     stderr="".join(list(capture.err_buf)),
                     killed_because=went_quiet,
                     duration_s=round(time.monotonic() - started_at, 3),
                     first_byte_s=rel(capture.first_byte_at if capture else None),
                     last_byte_s=rel(capture.last_byte_at if capture else None),
                     read_events=capture.read_events if capture else 0,
                     max_gap_s=round(capture.max_gap_s, 3) if capture else 0.0)


def _kill_group(proc: subprocess.Popen, sig, pgid: int | None = None) -> None:
    try:
        os.killpg(pgid if pgid is not None else os.getpgid(proc.pid), sig)
    except Exception:
        # os.kill, not proc.send_signal: this runs inside the SIGINT/SIGTERM
        # handler, and send_signal calls poll() -> waitpid on the way. A handler
        # must stay minimal, and reaping process state from inside one touches
        # exactly the state that is changing underneath it. Reading .returncode
        # is an attribute read, not a wait, and it keeps send_signal's real
        # guarantee: never signal a pid that has already been reaped and may
        # since belong to somebody else.
        try:
            if proc.returncode is None:
                os.kill(proc.pid, sig)
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
