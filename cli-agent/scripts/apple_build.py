"""Build and inspect images through Apple's container CLI."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from medulla.scripts import docker as shared

DEFAULT_CPUS = "4"
DEFAULT_MEMORY = "4g"
INTERRUPT_GRACE_S = 5


def resource_args() -> list[str]:
    cpus = os.environ.get("MEDULLA_APPLE_CPUS", DEFAULT_CPUS)
    memory = os.environ.get("MEDULLA_APPLE_MEMORY", DEFAULT_MEMORY)
    return ["--cpus", cpus, "--memory", memory]


def signal_process_group(proc: subprocess.Popen, signum: int) -> None:
    """Forward a signal only to the client process group owned by this run."""
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        return
    try:
        os.killpg(pgid, signum)
    except Exception:
        pass


def force_process_after_grace(proc: subprocess.Popen, wake: threading.Event,
                              grace_s: float = INTERRUPT_GRACE_S) -> None:
    wake.wait(grace_s)
    if proc.poll() is None:
        shared.terminate_process_group(proc, force=True)


def ensure_image(image: str, build: bool, workflow: str | None, cli_vars: dict,
                 dockerfile: Path | None = None, ready_image: bool = False) -> int:
    if not build:
        result = subprocess.run(
            ["container", "image", "inspect", image], capture_output=True, check=False)
        if result.returncode == 0:
            return 0
        if ready_image:
            print(f"image '{image}' not found locally, pulling...", file=sys.stderr)
            return subprocess.run(
                ["container", "image", "pull", image], check=False).returncode
        print(f"image '{image}' not found, building...", file=sys.stderr)

    dockerfile = dockerfile or shared.resolve_dockerfile(workflow, cli_vars)
    if not dockerfile.is_file():
        raise SystemExit(f"error: Dockerfile not found: {dockerfile}")
    context = Path.cwd()
    print(f"building image '{image}' from {context} (Dockerfile: {dockerfile})...",
          file=sys.stderr)
    cmd = ["container", "build", *resource_args(),
           "--build-arg", f"USER_UID={os.getuid()}",
           "-f", str(dockerfile), "-t", image]
    if build:
        cmd.append("--no-cache")
    cmd.append(str(context))
    interrupted = {"count": 0, "signal": signal.SIGINT}
    proc_ref: dict[str, subprocess.Popen | None] = {"proc": None}
    cleanup_ref: dict[str, threading.Thread | None] = {"thread": None}
    force_now = threading.Event()

    def start_build_cleanup(proc: subprocess.Popen) -> None:
        if cleanup_ref["thread"] is not None:
            return
        thread = threading.Thread(target=force_process_after_grace,
                                  args=(proc, force_now))
        cleanup_ref["thread"] = thread
        thread.start()

    def build_signal(signum, frame):
        interrupted["count"] += 1
        interrupted["signal"] = signum
        force = interrupted["count"] > 1
        label = "force stop" if force else "stopping"
        print(f"\n  ⏹ Apple container build interrupted ({label})",
              file=sys.stderr, flush=True)
        proc = proc_ref["proc"]
        if proc is None:
            return
        if force:
            force_now.set()
        else:
            signal_process_group(proc, signum)
            start_build_cleanup(proc)

    previous_int = signal.signal(signal.SIGINT, build_signal)
    previous_term = signal.signal(signal.SIGTERM, build_signal)
    try:
        if interrupted["count"]:
            return 130
        proc = subprocess.Popen(cmd, start_new_session=True)
        proc_ref["proc"] = proc
        if interrupted["count"]:
            if interrupted["count"] > 1:
                force_now.set()
            else:
                signal_process_group(proc, interrupted["signal"])
            start_build_cleanup(proc)
        proc.wait()
        force_now.set()
        if cleanup_ref["thread"] is not None:
            cleanup_ref["thread"].join()
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
    if interrupted["count"]:
        return 130
    if proc.returncode != 0:
        print("Apple container build failed", file=sys.stderr)
    return proc.returncode


def image_home(image: str, fallback: str = "/home/medulla") -> str:
    """Read the image user's HOME through Apple Container, with a safe fallback."""
    try:
        result = subprocess.run(
            ["container", "run", "--rm", "--entrypoint", "sh", image,
             "-c", 'printf %s "$HOME"'],
            capture_output=True, text=True, timeout=30, check=False)
        home = result.stdout.strip()
        if result.returncode == 0 and home.startswith("/") and home != "/":
            return home
    except (OSError, subprocess.TimeoutExpired):
        pass
    return fallback
