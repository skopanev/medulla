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
from dataclasses import dataclass
from pathlib import Path

from .model import TIMEOUT_RC


@dataclass
class RunResult:
    rc: int
    timed_out: bool
    stdout: str
    stderr: str


def run(
    command: str | list[str],
    cwd: Path,
    timeout_s: float,
    extra_env: dict[str, str] | None = None,
    log_path: Path | None = None,
    stdin_data: str | None = None,
    env_remove: list[str] | None = None,
    merge_stderr: bool = False,
) -> RunResult:
    if isinstance(command, str):
        shell = os.environ.get("SHELL", "bash")
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

    timed_out = False
    try:
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
