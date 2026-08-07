#!/usr/bin/env python3
"""Run Medulla workflows with Apple's `container` CLI."""
from __future__ import annotations

import os
import platform as platform  # re-exported for focused preflight tests
import shutil as shutil  # re-exported for focused preflight tests
import signal
import subprocess
import sys
import threading
import time
import uuid

from medulla.scripts import apple_build as apple_build
from medulla.scripts import docker as shared
from medulla.scripts.apple_build import ensure_image as ensure_image
from medulla.scripts.apple_build import (
    force_process_after_grace as force_process_after_grace,
)
from medulla.scripts.apple_build import resource_args
from medulla.scripts.apple_build import signal_process_group as signal_process_group
from medulla.scripts.apple_entry import (
    assert_runs_folder_reaches_container as assert_runs_folder_reaches_container,
)
from medulla.scripts.apple_entry import (
    container_service_running as container_service_running,
)
from medulla.scripts.apple_entry import main as _entry_main
from medulla.scripts.apple_entry import (
    plain_status_is_running as plain_status_is_running,
)
from medulla.scripts.dockerlib.process import HARNESS_ENV_KEYS

INTERRUPT_GRACE_S = 5


def kill_container(container_name: str, signal_name: str | None = None) -> None:
    cmd = ["container", "kill"]
    if signal_name:
        cmd.extend(["--signal", signal_name])
    cmd.append(container_name)
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def container_exists(container_name: str) -> bool:
    try:
        result = subprocess.run(
            ["container", "inspect", container_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.returncode == 0:
        return True
    return "container not found:" not in result.stderr.lower()


def delete_container(container_name: str) -> bool:
    """Delete this exact-name VM and verify that it actually disappeared."""
    for attempt in range(5):
        try:
            # Apple `container delete --force` may return 0 before the VM has
            # disappeared. Repeating this exact-name operation is idempotent.
            subprocess.run(
                ["container", "delete", "--force", container_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not container_exists(container_name):
            return True
        if attempt < 4:
            time.sleep(0.25)
    print(f"error: Apple container cleanup failed: {container_name} still exists",
          file=sys.stderr)
    return False


def force_cleanup_after_grace(container_name: str, proc: subprocess.Popen,
                              wake: threading.Event,
                              cleanup_failed: threading.Event,
                              grace_s: float = INTERRUPT_GRACE_S) -> None:
    """Give PID 1 grace, then remove only this run's exact-name VM."""
    wake.wait(grace_s)
    if proc.poll() is None:
        shared.terminate_process_group(proc, force=True)
    if not delete_container(container_name):
        cleanup_failed.set()


def schedule_force_cleanup(container_name: str, proc: subprocess.Popen,
                           wake: threading.Event,
                           cleanup_failed: threading.Event) -> threading.Thread:
    thread = threading.Thread(
        target=force_cleanup_after_grace,
        args=(container_name, proc, wake, cleanup_failed),
    )
    thread.start()
    return thread


def build_run_command(image: str, volumes: list[str], args: list[str],
                      container_name: str, run_dir_name: str | None = None,
                      runs_under: str | None = None) -> list[str]:
    cmd = ["container", "run", "--init", "--rm", "--name", container_name]
    cmd.extend(resource_args())
    if sys.stdin.isatty():
        cmd.append("-i")
    if shared.interactive_stdio():
        cmd.append("-t")

    for key in HARNESS_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            cmd.extend(["-e", f"{key}={value}"])
    if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
        cmd.extend(["-e", f"GEMINI_API_KEY={os.environ['GOOGLE_API_KEY']}"])
    if shared.dockerenv.env_file_for_run:
        cmd.extend(["--env-file", shared.dockerenv.env_file_for_run])

    cmd.extend(volumes)
    for path in shared.dockerpaths.shadow_paths_for_run:
        cmd.extend(["--tmpfs", f"/workspace/{path}"])
    cmd.extend(["-w", "/workspace"])
    # Existing harness adapters use this as the container-sandbox marker.
    cmd.extend(["-e", "MEDULLA_DOCKER=1"])
    if runs_under:
        cmd.extend(["-e", f"MEDULLA_RUNS_UNDER={runs_under}"])
    if run_dir_name:
        cmd.extend(["-e", f"MEDULLA_RUN_DIR_NAME={run_dir_name}"])
    cmd.extend([image, "medulla", *args])
    return cmd


def run_apple(image: str, volumes: list[str], args: list[str],
              runs_under: str | None = None,
              run_dir_name: str | None = None) -> int:
    container_name = f"medulla-{uuid.uuid4().hex[:8]}"
    cmd = build_run_command(image, volumes, args, container_name,
                            run_dir_name=run_dir_name, runs_under=runs_under)
    stdin = None if sys.stdin.isatty() else subprocess.DEVNULL
    interrupted = {"count": 0, "signal": signal.SIGINT}
    proc_ref: dict[str, subprocess.Popen | None] = {"proc": None}
    cleanup_ref: dict[str, threading.Thread | None] = {"thread": None}
    force_now = threading.Event()
    cleanup_failed = threading.Event()

    def stop_run(proc: subprocess.Popen, force: bool, signum: int) -> None:
        if not force:
            print(f"\n  stopping Apple container {container_name}...", file=sys.stderr, flush=True)
            signal_name = signal.Signals(signum).name.removeprefix("SIG")
            kill_container(container_name, signal_name)
            cleanup_ref["thread"] = schedule_force_cleanup(
                container_name, proc, force_now, cleanup_failed)
            return
        print(f"\n  force stopping Apple container {container_name}...", file=sys.stderr, flush=True)
        kill_container(container_name)
        force_now.set()

    def run_signal(signum, frame):
        interrupted["count"] += 1
        interrupted["signal"] = signum
        proc = proc_ref["proc"]
        if proc is None:
            return
        force = interrupted["count"] > 1
        stop_run(proc, force, signum)

    previous_int = signal.signal(signal.SIGINT, run_signal)
    previous_term = signal.signal(signal.SIGTERM, run_signal)
    rc = 130
    try:
        if not interrupted["count"]:
            proc = subprocess.Popen(cmd, stdin=stdin, start_new_session=True)
            proc_ref["proc"] = proc
            if interrupted["count"]:
                force = interrupted["count"] > 1
                stop_run(proc, force, interrupted["signal"])
            proc.wait()
            rc = 130 if interrupted["count"] else proc.returncode
    finally:
        if interrupted["count"] and proc_ref["proc"] is not None:
            thread = cleanup_ref["thread"]
            if thread is None:
                thread = schedule_force_cleanup(
                    container_name, proc_ref["proc"], force_now, cleanup_failed)
            thread.join()
        shared._unlink_env_file()
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
    return 1 if cleanup_failed.is_set() else rc


def main() -> int:
    return _entry_main(run_apple)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
