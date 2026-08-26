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

# every live child, registered for signal-time group-kill: an interrupt must
# reach POOL WORKERS' children too (the exception only lands in the main
# thread; workers sit in proc.wait until their agents die)
_LIVE: set = set()
_LIVE_LOCK = threading.Lock()


def kill_live_processes() -> None:
    """Signal-handler duty: SIGTERM every registered child's process group so
    worker threads unblock immediately and the engine can conclude."""
    with _LIVE_LOCK:
        procs = list(_LIVE)
    for proc in procs:
        _kill_group(proc, signal.SIGTERM)


@dataclass
class RunResult:
    rc: int
    timed_out: bool
    stdout: str
    stderr: str


FIRST_OUTPUT_S = 60      # every harness CLI prints an init event well inside this
# And then it keeps talking. Measured on a healthy opencode round: 298 events in 586
# seconds, median gap 0s, 90th percentile 3s, longest 66s — no pause over two minutes
# in the whole run. Five minutes is generous by a factor of four and still catches a
# body that died mid-work, which is what a live panel did: three panelists went quiet
# for 10-14 minutes each and burned a 1800s timeout, then a retry burned another.
IDLE_OUTPUT_S = 300


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
) -> RunResult:
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
    log_lock = threading.Lock()

    proc = subprocess.Popen(
        argv, cwd=str(cwd),
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        text=True, bufsize=1, start_new_session=True, env=env,
        errors="replace",
    )
    with _LIVE_LOCK:
        _LIVE.add(proc)
    if stdin_data is not None:
        # write+close in a thread: a child that never reads must not deadlock us
        def _feed():
            try:
                proc.stdin.write(stdin_data)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        threading.Thread(target=_feed, daemon=True).start()

    out_buf: list[str] = []
    err_buf: list[str] = []

    def pump(pipe, buf, tag):
        try:
            for line in iter(pipe.readline, ""):
                buf.append(line)
                if log_file:
                    with log_lock:
                        log_file.write(f"[{tag}] {line}")
                if echo is not None:
                    try:
                        echo(tag, line)
                    except Exception:
                        pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    t_out = threading.Thread(target=pump, args=(proc.stdout, out_buf, "out"), daemon=True)
    t_out.start()
    if proc.stderr is not None:
        t_err = threading.Thread(target=pump, args=(proc.stderr, err_buf, "err"), daemon=True)
        t_err.start()
    else:
        t_err = None

    # A CLI that started says SOMETHING within seconds — a session id, an init event,
    # a hook response. Silence is not deep thought, it is a process that never came up:
    # a wrapper that died before its first write, a provider handshake hanging, a
    # binary waiting on a tty nobody attached. Waiting out the full body timeout to
    # discover that costs the whole budget — and then the retry costs it again.
    #
    # So: nothing at all after FIRST_OUTPUT_S, and the attempt ends early with a
    # failure that IS worth retrying, unlike a timeout at the far end.
    went_quiet = ""
    if watch_output and timeout_s > FIRST_OUTPUT_S * 2:
        went_quiet = _watch_output(proc, out_buf, err_buf, timeout_s)

    timed_out = False
    try:
        if went_quiet:
            raise subprocess.TimeoutExpired(argv, timeout_s)
        proc.wait(timeout=timeout_s if timeout_s > 0 else 0.001)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _kill_group(proc, signal.SIGKILL)
            proc.wait()
    except BaseException:
        # KeyboardInterrupt or anything else: the child MUST NOT outlive us —
        # it sits in its own session (start_new_session) and nobody else will
        # kill it (audit R1: v1 had this, the rewrite lost it)
        _kill_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_group(proc, signal.SIGKILL)
            proc.wait()
        raise
    finally:
        # generous join: an agent's daemon grandchild can hold the pipe open;
        # 5s truncated real output (audit G7). The child itself is already dead
        # here, so this only bounds pipe-drain time.
        t_out.join(timeout=60)
        if t_err is not None:
            t_err.join(timeout=60)
        if log_file:
            log_file.close()
        if proc.poll() is None:                    # belt & braces: never leak
            _kill_group(proc, signal.SIGKILL)
        with _LIVE_LOCK:
            _LIVE.discard(proc)

    rc = TIMEOUT_RC if timed_out else proc.returncode
    return RunResult(rc=rc, timed_out=timed_out, stdout="".join(out_buf), stderr="".join(err_buf))


def _kill_group(proc: subprocess.Popen, sig) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except Exception:
        try:
            proc.send_signal(sig)
        except Exception:
            pass


def _watch_output(proc, out_buf: list, err_buf: list, timeout_s: float) -> str:
    """Wait for the child, but not through silence. Returns why we stopped, or "".

    ONLY for agent CLIs, which announce themselves and then narrate: claude a
    session_id, codex thread.started, opencode step_start, agy init. A shell body is
    any program at all — curl, tar, a compiler, an API client that speaks once it is
    done — and silence there is not a symptom of anything. Applying this to shell
    killed a fetcher at 60 seconds that normally runs 140-180 and was working fine.

    Two thresholds, because they mean different things. Nothing at all in the first
    minute is a process that never came up — a wrapper dead before its first write, a
    handshake hanging, a binary waiting on a tty. Output that STOPS for five minutes is
    a body that died mid-work, and it will not resume: a healthy round writes an event
    every few seconds.

    Either way the caller sees a timeout, which is what it is — just discovered in
    minutes rather than at the far end of a half-hour budget.
    """
    end = time.monotonic() + timeout_s
    seen = 0
    last = time.monotonic()
    while time.monotonic() < end:
        if proc.poll() is not None:
            return ""                       # finished on its own
        now_seen = len(out_buf) + len(err_buf)
        if now_seen > seen:
            seen, last = now_seen, time.monotonic()
        quiet = time.monotonic() - last
        if seen == 0 and quiet > FIRST_OUTPUT_S:
            return f"no output at all in {FIRST_OUTPUT_S}s"
        if seen > 0 and quiet > IDLE_OUTPUT_S:
            return f"silent for {IDLE_OUTPUT_S}s after {seen} lines"
        time.sleep(0.25)
    return ""                               # the real timeout takes it from here
