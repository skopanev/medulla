#!/usr/bin/env python3
"""Run Medulla workflows with Apple's `container` CLI."""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from medulla.scripts import docker as shared

DEFAULT_IMAGE = "medulla:latest"
DEFAULT_CPUS = "4"
DEFAULT_MEMORY = "4g"
INTERRUPT_GRACE_S = 5


def resource_args() -> list[str]:
    cpus = os.environ.get("MEDULLA_APPLE_CPUS", DEFAULT_CPUS)
    memory = os.environ.get("MEDULLA_APPLE_MEMORY", DEFAULT_MEMORY)
    return ["--cpus", cpus, "--memory", memory]


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


def force_process_after_grace(proc: subprocess.Popen, wake: threading.Event,
                              grace_s: float = INTERRUPT_GRACE_S) -> None:
    """Force only this client group after grace, or immediately when woken."""
    wake.wait(grace_s)
    if proc.poll() is None:
        shared.terminate_process_group(proc, force=True)


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


def build_run_command(image: str, volumes: list[str], args: list[str],
                      container_name: str) -> list[str]:
    cmd = ["container", "run", "--init", "--rm", "--name", container_name]
    cmd.extend(resource_args())
    if sys.stdin.isatty():
        cmd.append("-i")
    if shared.interactive_stdio():
        cmd.append("-t")

    for key in shared.HARNESS_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            cmd.extend(["-e", f"{key}={value}"])
    if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
        cmd.extend(["-e", f"GEMINI_API_KEY={os.environ['GOOGLE_API_KEY']}"])
    if shared.env_file_for_run:
        cmd.extend(["--env-file", shared.env_file_for_run])

    cmd.extend(volumes)
    for path in shared.shadow_paths_for_run:
        cmd.extend(["--tmpfs", f"/workspace/{path}"])
    cmd.extend(["-w", "/workspace"])
    # Existing harness adapters use this as the container-sandbox marker.
    cmd.extend(["-e", "MEDULLA_DOCKER=1"])
    cmd.extend([image, "medulla", *args])
    return cmd


def ensure_image(image: str, build: bool, workflow: str | None, cli_vars: dict,
                 dockerfile: Path | None = None, ready_image: bool = False) -> int:
    if not build:
        result = subprocess.run(
            ["container", "image", "inspect", image], capture_output=True, check=False)
        if result.returncode == 0:
            return 0
        if ready_image:
            print(f"image '{image}' not found locally, pulling...", file=sys.stderr)
            return subprocess.run(["container", "image", "pull", image], check=False).returncode
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
        thread = threading.Thread(
            target=force_process_after_grace,
            args=(proc, force_now),
        )
        cleanup_ref["thread"] = thread
        thread.start()

    def build_signal(signum, frame):
        interrupted["count"] += 1
        interrupted["signal"] = signum
        force = interrupted["count"] > 1
        label = "force stop" if force else "stopping"
        print(f"\n  ⏹ Apple container build interrupted ({label})", file=sys.stderr, flush=True)
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
        # The Apple BuildKit builder is shared; never stop it for one client.
        return 130
    if proc.returncode != 0:
        print("Apple container build failed", file=sys.stderr)
    return proc.returncode


def run_apple(image: str, volumes: list[str], args: list[str]) -> int:
    container_name = f"medulla-{uuid.uuid4().hex[:8]}"
    cmd = build_run_command(image, volumes, args, container_name)
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


def container_service_running() -> bool:
    status = subprocess.run(
        ["container", "system", "status", "--format", "json"],
        capture_output=True,
        check=False,
        text=True,
    )
    if status.returncode == 0:
        try:
            return json.loads(status.stdout)["status"] == "running"
        except (KeyError, TypeError, json.JSONDecodeError):
            return plain_status_is_running(status.stdout)

    # Older container releases did not expose formatted status output.
    status = subprocess.run(
        ["container", "system", "status"], capture_output=True, check=False, text=True)
    return status.returncode == 0 and plain_status_is_running(status.stdout)


def plain_status_is_running(output: str) -> bool:
    for line in output.lower().splitlines():
        fields = line.replace(":", " ").split()
        if fields and fields[0] == "status":
            return len(fields) > 1 and fields[1] == "running"
    lowered = output.lower()
    return "is running" in lowered and "not running" not in lowered


def main() -> int:
    if sys.platform != "darwin":
        print("error: Apple container runtime requires macOS", file=sys.stderr)
        return 1
    if platform.machine() != "arm64":
        print("error: Apple container runtime requires Apple silicon", file=sys.stderr)
        return 1
    mac_version = platform.mac_ver()[0]
    try:
        mac_major = int(mac_version.split(".", 1)[0])
    except (AttributeError, ValueError):
        mac_major = 0
    if mac_major < 26:
        print("error: Apple container runtime requires macOS 26 or newer", file=sys.stderr)
        return 1
    if shutil.which("container") is None:
        print("error: Apple `container` CLI not found; install it from github.com/apple/container",
              file=sys.stderr)
        return 1
    if not container_service_running():
        print("error: Apple container service is not running; run `container system start`",
              file=sys.stderr)
        return 1

    args = sys.argv[1:]
    build = "--build" in args
    if build:
        args = [arg for arg in args if arg != "--build"]

    extra_mounts: list[tuple[str, bool]] = []
    cli_vars: dict[str, str] = {}
    clean_args: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in ("--mount", "--mount-rw") and i + 1 < len(args):
            extra_mounts.append((args[i + 1], args[i] == "--mount"))
            i += 2
        elif args[i] == "--var" and i + 1 < len(args):
            value = args[i + 1]
            if "=" in value:
                key, item = value.split("=", 1)
                cli_vars[key] = item
            clean_args.extend(args[i:i + 2])
            i += 2
        else:
            clean_args.append(args[i])
            i += 1
    args = clean_args

    workflow = None
    for i, arg in enumerate(args):
        if arg in ("-w", "--workflow") and i + 1 < len(args):
            workflow = args[i + 1]
            break

    dockerfile = None
    workflow_vars = shared.read_workflow_vars(workflow)
    image = (os.environ.get("MEDULLA_IMAGE") or cli_vars.get("IMAGE")
             or workflow_vars.get("IMAGE"))
    if image is None:
        if workflow:
            dockerfile = shared.resolve_dockerfile(workflow, cli_vars)
            if not dockerfile.is_file():
                raise SystemExit(f"error: Dockerfile not found: {dockerfile}")
            image = shared.image_tag_for(workflow, dockerfile)
        else:
            image = DEFAULT_IMAGE

    rc = ensure_image(image, build, workflow, cli_vars, dockerfile=dockerfile,
                      ready_image=dockerfile is None and workflow is not None)
    if rc != 0:
        return rc

    claude_config = os.environ.get("CLAUDE_CONFIG_DIR")
    claude_home = (Path(claude_config).expanduser().resolve() if claude_config
                   else Path.home() / ".claude")
    shared.shadow_paths_for_run = shared.read_shadow_paths(workflow)
    dotenv = shared._collect_dotenv(workflow)
    if dotenv:
        import atexit
        import tempfile
        fd, shared.env_file_for_run = tempfile.mkstemp(prefix="medulla-env-")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            for key, value in dotenv.items():
                handle.write(f"{key}={value}\n")
        atexit.register(shared._unlink_env_file)

    volumes = shared.build_volumes(claude_home, mount_agy=shared.workflow_uses_agy(workflow))
    for mount_path, read_only in extra_mounts:
        path = Path(mount_path).resolve()
        if not path.is_dir():
            print(f"[apple_container.py] mount path not found: {path}", file=sys.stderr)
            return 1
        suffix = ":ro" if read_only else ""
        volumes.extend(["-v", f"{path}:/workspace/{path.name}{suffix}"])
    return run_apple(image, volumes, args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
